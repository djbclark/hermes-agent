"""No-agent scheduled drain for the durable memory journal.

The scheduler expects a script that is silent when there is no work and emits
one concise line when records were processed or failed.  The actual consumer
and lease semantics live in :mod:`tools.memory_projection`.

When a drain would land above TARGET_PCT, this worker calls a low-cost LLM
(opencode-go, else deepseek/deepseek-v4-flash — never Claude) to consolidate
CONTENT, then applies. apply_with_capacity never FIFO-deletes durable entries.

On every run this script also appends a metrics snapshot (one JSON line) to
``~/.hermes/logs/memory_journal_metrics.jsonl`` for external monitoring, and
rotates that file when it exceeds 100 KB.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tools import memory_projection
from tools.memory_tool import (
    ENTRY_DELIMITER,
    MEMORY_TARGET_PCT,
    entries_char_total,
    get_memory_dir,
    load_on_disk_store,
    target_char_budget,
)

logger = logging.getLogger(__name__)

# Bounded, cheap, non-Claude. Prefer free/quiet opencode-go when reachable.
_CONSOLIDATION_MAX_TOKENS = 1024
_CONSOLIDATION_TIMEOUT_SECONDS = 45.0
_CONSOLIDATION_CANDIDATES: Tuple[Tuple[str, Optional[str]], ...] = (
    ("opencode-go", None),
    ("deepseek", "deepseek-v4-flash"),
    ("deepseek", "deepseek/deepseek-v4-flash"),
)
_METRICS_PATH = Path.home() / ".hermes" / "logs" / "memory_journal_metrics.jsonl"
_MAX_METRICS_BYTES = 100_000  # 100 KB

# Alert thresholds (Phase A): produce user-visible output when exceeded.
_STALE_PENDING_ALERT_SECONDS = 900  # 15 minutes
_FALLBACK_JOURNAL_PATH = "pending_fallback.jsonl"


def _provider_reachable(provider: str, model: Optional[str] = None) -> bool:
    """True when credentials resolve to a live client for *provider*."""
    try:
        from agent.auxiliary_client import resolve_provider_client
        client, _resolved = resolve_provider_client(provider, model=model)
    except Exception:
        return False
    return client is not None


def _select_consolidation_model() -> Optional[Tuple[str, str]]:
    """Prefer opencode-go if reachable; else deepseek-v4-flash. Never Claude."""
    for provider, model in _CONSOLIDATION_CANDIDATES:
        if provider.lower() == "anthropic" or (model or "").lower().startswith("claude"):
            continue
        if not _provider_reachable(provider, model):
            continue
        resolved = model
        if not resolved:
            try:
                from agent.auxiliary_client import resolve_provider_client
                _client, resolved = resolve_provider_client(provider, model=model)
            except Exception:
                resolved = None
        if not resolved:
            # opencode-go's default aux model is glm-5
            resolved = "glm-5" if provider == "opencode-go" else "deepseek-v4-flash"
        return provider, resolved
    return None


def _parse_consolidated_entries(content: str) -> Optional[List[str]]:
    text = (content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        fence = text.rfind("```")
        if fence >= 0:
            text = text[:fence]
        text = text.strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        data = data["entries"]
    if isinstance(data, list) and all(isinstance(item, str) for item in data):
        parsed = [item.strip() for item in data if item and item.strip()]
        return parsed or None
    if ENTRY_DELIMITER in text:
        parsed = [item.strip() for item in text.split(ENTRY_DELIMITER) if item.strip()]
        return parsed or None
    return [text] if text else None


def _format_queued_operations(operations: Optional[Sequence[Dict[str, Any]]]) -> str:
    if not operations:
        return "(none)"
    return json.dumps(list(operations), ensure_ascii=False, indent=2)


def _consolidation_prompt(
    entries: Sequence[str],
    *,
    target: str,
    target_chars: int,
    queued_operations: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    store_text = ENTRY_DELIMITER.join(entries)
    return (
        "Consolidate the bounded memory store below.\n"
        f"Target store: {target}\n"
        f"Hard budget: {target_chars} characters including the delimiter "
        f"{ENTRY_DELIMITER!r} between entries ({MEMORY_TARGET_PCT}% hysteresis target).\n"
        "Rules:\n"
        "- Preserve every durable fact. Do not invent facts.\n"
        "- Merge near-duplicate CONTENT entries. Summarize oldest CONTENT if needed.\n"
        "- Never delete a unique fact. Never drop an entry that starts with [pinned].\n"
        "- Incorporate the queued write if it adds a new durable fact.\n"
        "- Return ONLY a JSON array of strings (the consolidated entries). No commentary.\n"
        "\nCURRENT STORE:\n"
        f"{store_text}\n"
        "\nQUEUED WRITE:\n"
        f"{_format_queued_operations(queued_operations)}\n"
    )


def llm_capacity_consolidator(
    entries,
    target,
    limit,
    protected,
    target_chars,
    queued_operations=None,
):
    """LLM consolidator installed on MemoryStore.capacity_consolidator.

    Returns a list of entries at or under *target_chars*, or None so
    apply_with_capacity can fall back to deterministic merge/summarize.
    """
    del protected  # apply_with_capacity re-checks pinned/protected facts
    selected = _select_consolidation_model()
    if selected is None:
        logger.info("memory journal: no low-cost consolidator reachable")
        return None
    provider, model = selected
    try:
        from agent.auxiliary_client import call_llm
        from tools.threat_patterns import first_threat_message
    except Exception:
        logger.exception("memory journal: cannot import consolidator dependencies")
        return None

    prompt = _consolidation_prompt(
        entries,
        target=target,
        target_chars=target_chars,
        queued_operations=queued_operations,
    )
    try:
        resp = call_llm(
            provider=provider,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=_CONSOLIDATION_MAX_TOKENS,
            timeout=_CONSOLIDATION_TIMEOUT_SECONDS,
            reasoning_config={"enabled": False},
        )
        content = resp.choices[0].message.content
        if not isinstance(content, str):
            content = str(content) if content else ""
    except Exception:
        logger.exception(
            "memory journal: consolidator call failed (%s/%s)", provider, model
        )
        return None

    parsed = _parse_consolidated_entries(content)
    if not parsed:
        return None
    if any(first_threat_message(item, scope="strict") for item in parsed):
        logger.warning("memory journal: consolidator output failed threat scan")
        return None
    if entries_char_total(parsed) > max(target_chars, 0) and entries_char_total(parsed) > limit:
        return None
    logger.info(
        "memory journal: consolidated %s via %s/%s (%s -> %s chars, target %s)",
        target, provider, model,
        entries_char_total(list(entries)), entries_char_total(parsed), target_chars,
    )
    return parsed


def _pre_drain_llm_consolidate(store) -> None:
    """If the live store is above TARGET_PCT with pending overflow, consolidate first."""
    try:
        from tools import memory_pending_queue as pq
        active = pq.list_active(kind=pq.KIND_OVERFLOW)
    except Exception:
        return
    if not active:
        return

    for target in ("memory", "user"):
        try:
            usage = int(store._usage_pct(target))
        except (TypeError, ValueError, AttributeError):
            continue
        if usage <= MEMORY_TARGET_PCT:
            continue
        queued = [row for row in active if row.get("target") == target]
        if not queued:
            continue
        try:
            limit = int(store._char_limit(target))
            entries = list(store._entries_for(target))
        except (TypeError, ValueError, AttributeError):
            continue
        operations = []
        for row in queued:
            payload = dict(row.get("payload") or {})
            payload["action"] = row.get("action")
            operations.append(payload)
        candidate = llm_capacity_consolidator(
            entries,
            target,
            limit,
            set(),
            target_char_budget(limit),
            queued_operations=operations,
        )
        if not candidate or entries_char_total(candidate) > limit:
            continue
        try:
            store._set_entries(target, candidate)
            store.save_to_disk(target)
        except Exception:
            logger.exception("memory journal: failed to persist pre-drain consolidation")


def _capacity_dead_alerts(results: Sequence[Dict[str, Any]]) -> List[str]:
    dead = []
    for row in results:
        if row.get("outcome") != "dead":
            continue
        error = (row.get("error") or "").lower()
        if (
            "cannot make room" in error
            or "consolidation" in error
            or "unresolvable" in error
        ):
            dead.append(row)
    if not dead:
        return []
    return [
        "ALERT: memory consolidation could not free room; "
        f"{len(dead)} write(s) dead-lettered. Review with /memory pending."
    ]


def _append_metrics_line(status: dict) -> None:
    """Append one JSON line to the metrics file, rotating at 100 KB."""
    store = load_on_disk_store()
    line = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "active_count": status["active_count"],
        "pending_count": status["pending_count"],
        "processing_count": status["processing_count"],
        "failed_count": status["failed_count"],
        "dead_letter_count": status["dead_letter_count"],
        "evicted_count": status["evicted_count"],
        "memory_char_used": len(ENTRY_DELIMITER.join(store.memory_entries)),
        "memory_char_limit": store.memory_char_limit,
        "user_char_used": len(ENTRY_DELIMITER.join(store.user_entries)),
        "user_char_limit": store.user_char_limit,
        "behind": status["behind"],
    }

    _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Rotate: keep only one backup at .1
    if _METRICS_PATH.exists() and _METRICS_PATH.stat().st_size >= _MAX_METRICS_BYTES:
        rotated = _METRICS_PATH.with_suffix(".jsonl.1")
        shutil.move(str(_METRICS_PATH), str(rotated))

    with open(_METRICS_PATH, "a", encoding="utf-8") as f:
        json.dump(line, f)
        f.write("\n")


def _format_metrics_summary(status: dict) -> str:
    """Build the one-line metrics summary string."""
    store = load_on_disk_store()
    memory_used = len(ENTRY_DELIMITER.join(store.memory_entries))
    user_used = len(ENTRY_DELIMITER.join(store.user_entries))
    return (
        f"memory journal: ok, "
        f"{status['active_count']} active, "
        f"{status['dead_letter_count']} dead, "
        f"{status['evicted_count']} evicted, "
        f"{memory_used}/{store.memory_char_limit} memory, "
        f"{user_used}/{store.user_char_limit} user"
    )


def main() -> int:
    store = load_on_disk_store()
    try:
        store.capacity_consolidator = llm_capacity_consolidator
        _pre_drain_llm_consolidate(store)
    except Exception:
        logger.exception("memory journal: consolidator install/pre-drain failed")

    result = memory_projection.run_once(max_records=100, store=store)
    processed = int(result.get("processed", 0))
    counts = result.get("counts", {})
    failures = [
        row for row in result.get("results", [])
        if row.get("outcome") not in {"done"}
    ]

    status = memory_projection.get_status()
    _append_metrics_line(status)

    alerts = _check_alerts(status)
    alerts.extend(_capacity_dead_alerts(result.get("results") or []))

    # Healthy idle polls remain silent; metrics are retained locally for
    # watchdogs and summaries. Only actual work, failures, or alerts are
    # user-visible.
    if not processed and not failures and not alerts:
        return 0

    parts = []
    if processed:
        parts.append(f"Hermes memory journal processed {processed} record(s).")
    if counts:
        parts.append(" ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    if failures:
        parts.append(
            "Failures: "
            + "; ".join(
                f"{row.get('id')} {row.get('outcome')}: {row.get('error', '')}"
                for row in failures
            )
        )
    if alerts:
        parts.extend(alerts)
    print(" ".join(parts))
    return 0


def _check_alerts(status: dict) -> list:
    """Return alert strings for stale pending records or fallback journal use."""
    alerts = []
    now = time.time()

    # Check for records pending longer than the alert threshold.
    oldest_age = status.get("oldest_age_seconds", 0)
    active_count = status.get("active_count", 0)
    if active_count > 0 and oldest_age > _STALE_PENDING_ALERT_SECONDS:
        minutes = int(oldest_age / 60)
        alerts.append(
            f"ALERT: {active_count} memory journal record(s) pending for "
            f">{minutes} min. Review with /memory pending or let the "
            f"projection consumer apply them."
        )

    # Check for fallback-journal entries (SQLite queue was unavailable).
    fallback = get_memory_dir() / _FALLBACK_JOURNAL_PATH
    if fallback.exists():
        try:
            size = fallback.stat().st_size
            if size > 0:
                alerts.append(
                    f"ALERT: fallback memory journal exists ({size} bytes). "
                    f"The SQLite pending queue was unavailable at least once. "
                    f"Restore with: hermes memory import-fallback"
                )
        except OSError:
            pass

    return alerts


if __name__ == "__main__":
    raise SystemExit(main())
