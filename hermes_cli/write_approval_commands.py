#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from tools import memory_pending_queue as pq
from tools import memory_pending_migration as mpm
from tools import memory_projection as mp
from tools import write_approval as wa
from tools.memory_tool import get_memory_dir

logger = logging.getLogger(__name__)


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    return f"{subsystem}.write_approval = {'on' if on else 'off'}"


def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
    lines.append("")
    lines.append(f"Apply: /{subsystem} approve <id>   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    return "\n".join(lines)


def handle_pending_subcommand(
    subsystem: str, args: List[str], *, memory_store=None, set_mode_fn=None) -> Optional[str]:
    """Dispatch a /memory or /skills write-approval subcommand.

    ``memory_store`` applies approved memory writes (CLI passes its live store; gateway a freshly
    loaded one); ``set_mode_fn`` persists the write_approval boolean. Returns text for the user,
    or None when the args are not a write-approval subcommand so the caller falls through to its
    other handling (e.g. /skills search).
    """
    if not args:
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)
    sub, rest = args[0].lower(), args[1:]
    if sub == "pending":
        return _fmt_pending_list(subsystem)
    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)
    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)
    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)
    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)

    # -- journal / eviction / migration / fallback (memory only) --
    if sub == "migrate" and subsystem == wa.MEMORY:
        return _migrate()
    if sub == "evicted" and subsystem == wa.MEMORY:
        return _evicted()
    if sub == "journal" and subsystem == wa.MEMORY:
        return _journal()
    if sub == "import-fallback" and subsystem == wa.MEMORY:
        return _import_fallback()

    return None  # not ours — caller handles


def _usage(subsystem: str) -> str:
    return f"Usage: /{subsystem} approve|reject <id>  (or 'all')"


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    if not rest:
        return _usage(subsystem)
    target = rest[0]
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [rec]

    applied, failed = 0, []
    for rec in targets:
        ok, msg = _apply_one(subsystem, rec, memory_store)
        if ok:
            wa.discard_pending(subsystem, rec["id"])
            applied += 1
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _apply_one(subsystem: str, rec, memory_store):
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable"
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
        return bool(result.get("success")), result.get("error", "")
    except Exception as e:
        return False, str(e)


def _reject(subsystem: str, rest: List[str]) -> str:
    if not rest:
        return _usage(subsystem)
    target = rest[0]
    if target.lower() == "all":
        n = sum(1 for rec in wa.list_pending(subsystem) if wa.discard_pending(subsystem, rec["id"]))
        return f"Rejected {n} pending {subsystem} write(s)."
    if wa.discard_pending(subsystem, target):
        return f"Rejected pending {subsystem} write '{target}'."
    return f"No pending {subsystem} write with id '{target}'."


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    return f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n\n" + wa.skill_pending_diff(rec)


_APPROVAL_VALUES = {
    **dict.fromkeys(("on", "true", "yes", "1", "enable", "enabled"), True),
    **dict.fromkeys(("off", "false", "no", "0", "disable", "disabled"), False)}


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem."""
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                f"Set with: /{subsystem} approval <on|off>")
    arg = rest[0].strip().lower()
    enabled = _APPROVAL_VALUES.get(arg)
    if enabled is None:
        return f"Invalid value '{arg}'. Use: on or off."
    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return (f"To change the {subsystem} approval gate, run:\n"
                f"  hermes config set {subsystem}.write_approval {val}")
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set {subsystem}.write_approval: {e}"
    return f"{subsystem}.write_approval set to '{'on' if enabled else 'off'}'."


# ---------------------------------------------------------------------------
# Journal / eviction / migration handlers (memory only)
# ---------------------------------------------------------------------------


def _truncate_line(text: str, max_len: int = 120) -> str:
    """Truncate a line for chat-bubble display."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _migrate() -> str:
    """Run legacy-to-SQLite pending migration."""
    result = mpm.migrate_legacy_pending()
    migrated = len(result.get("migrated", []))
    failed = len(result.get("failed", []))
    if migrated == 0 and failed == 0:
        return "Legacy migration: nothing to migrate (no legacy files found)."
    out = [f"Legacy migration: {migrated} file(s) imported."]
    if failed:
        out.append(f"{failed} file(s) failed (will retry on next run):")
        for f in result["failed"][:5]:
            out.append(f"  - {f.get('file', '?')}")
        if len(result["failed"]) > 5:
            out.append(f"  ... and {len(result['failed']) - 5} more")
    return "\n".join(out)


def _evicted() -> str:
    """List dead-lettered and evicted entries (truncated for chat bubbles)."""
    dead = pq.list_all(status=pq.STATUS_DEAD)
    evicted = pq.list_evictions(limit=10)

    lines = []
    if dead:
        lines.append(f"Dead-lettered ({len(dead)}):")
        for r in dead[-10:]:
            kind = r.get("kind", "?")
            summary = str(r.get("summary") or r.get("payload", {}))
            if isinstance(summary, dict):
                summary = str(summary)[:80]
            line = f"  {r.get('id', '?')}  {kind}  {summary[:80]}"
            lines.append(_truncate_line(line))
    else:
        lines.append("Dead-lettered: 0")

    if evicted:
        lines.append(f"\nRecent evictions ({len(evicted)}):")
        for r in evicted:
            reason = str(r.get("reason") or "")[:80]
            line = f"  {r.get('id', '?')}  {r.get('target', '?')}  {reason}"
            lines.append(_truncate_line(line))
    else:
        lines.append("\nRecent evictions: 0")

    return "\n".join(lines)


def _journal() -> str:
    """Show projection journal status summary."""
    status = mp.get_status()
    fallback = status.get("fallback")
    if fallback is None:
        try:
            fallback_path = get_memory_dir() / "pending_fallback.jsonl"
            fallback = fallback_path.exists() and fallback_path.stat().st_size > 0
        except OSError:
            fallback = False
    lines = [
        "Memory journal status:",
        f"  Active: {status['active_count']}",
        f"  Pending: {status['pending_count']}",
        f"  Processing: {status['processing_count']}",
        f"  Failed: {status['failed_count']}",
        f"  Dead-lettered: {status['dead_letter_count']}",
        f"  Oldest age: {status['oldest_age_seconds']:.0f}s",
        f"  Behind: {'yes' if status['behind'] else 'no'}",
        f"  Fallback: {'yes' if fallback else 'no'}",
    ]
    if status.get("last_error"):
        lines.append(f"  Last error: {_truncate_line(str(status['last_error']), 120)}")
    return "\n".join(lines)


def _import_fallback() -> str:
    """Import pending records from the fallback JSONL journal into SQLite."""
    from pathlib import Path
    import json
    from tools import memory_pending_queue as pq
    from tools.memory_tool import get_memory_dir

    fallback = get_memory_dir() / "pending_fallback.jsonl"
    if not fallback.exists():
        return "No fallback journal found."

    imported = 0
    failed = 0
    try:
        with open(fallback, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    # Re-enqueue into SQLite queue
                    pq.enqueue(
                        kind=pq.KIND_OVERFLOW,
                        action=record.get("action"),
                        target=record.get("target"),
                        payload=record.get("payload"),
                        summary=record.get("summary", "fallback import"),
                        expected_previous_hash=record.get("expected_previous_hash"),
                        origin="fallback_import",
                    )
                    imported += 1
                except Exception as e:
                    failed += 1
                    logger.warning("Fallback import failed for line: %s", e)

        # Rename the fallback file so we don't re-import on next run
        if imported > 0:
            fallback.rename(fallback.with_suffix(".jsonl.imported"))

    except Exception as e:
        return f"Fallback import failed: {e}"

    return f"Imported {imported} fallback record(s) into SQLite queue" + (f", {failed} failed" if failed else ".")
