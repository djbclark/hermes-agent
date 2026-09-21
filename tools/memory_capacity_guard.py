"""Memory capacity guard -- fork-only layer over the upstream ``MemoryStore``.

Everything here is owned by the fork (djbclark/hermes-agent); upstream has no
equivalent. It exists as its own module so that ``tools/memory_tool.py`` and
``tools/memory_tool_store.py`` can track upstream verbatim and stop conflicting
on every merge. ``tools/memory_tool.py`` carries only the small hooks tagged
``# FORK(memory-capacity-guard):`` (grep for that tag to find / re-apply them).

Capacity guard (Phase A): when a write would exceed the store's character limit,
it is durably journaled to a SQLite pending-operation queue instead of being
rejected. The agent receives a ``queued`` result with the operation ID rather
than a ``success: false`` error, so it does not retry or lose the fact.

  - WARN at 75 %  -- usage note is appended to success responses but the write
                     still lands in the live store.
  - JOURNAL at 85 % -- capacity-increasing writes are durably queued instead of
                     applied directly; non-increasing writes (remove, shorter
                     replace) still land live.
  - PROJECT at <=70 % -- the projection consumer's target after journaled entries
                     are applied. Drain consolidates (merge/summarize CONTENT);
                     it never deletes durable entries.
  - Hysteresis: journal mode latches at QUEUE_PCT and stays latched while
                usage is above TARGET_PCT and overflow work is still pending.

Design: :class:`GuardedMemoryStore` subclasses the upstream ``MemoryStore`` and
intercepts the single ``_mutate`` funnel (plus the public ``add`` / ``_edit`` /
``apply_batch`` entry points, which only record which operation is running).
When upstream's own validation reports an over-limit failure and the store is
in journal mode, the failure is replaced by a journaled ``queued`` result.
"""

import contextvars
import logging
import time
from typing import Any, Dict, List, Optional

from tools.memory_tool_store import (
    ENTRY_DELIMITER,
    MemoryStore as _UpstreamMemoryStore,
    _drift_error,
    _read_failed_error,
    _scan_memory_content,
)

logger = logging.getLogger("tools.memory_tool")


# Capacity guard thresholds (Phase A):
#   WARN_PCT  — include a usage note in success responses, write still lands.
#   QUEUE_PCT — journal capacity-increasing writes to the pending queue instead
#               of applying directly; non-increasing writes still land live.
#   TARGET_PCT — drain / hysteresis low-water mark. Journal mode latches at
#               QUEUE_PCT and stays latched until usage falls to TARGET_PCT.
MEMORY_WARN_PCT = 75
MEMORY_QUEUE_PCT = 85
MEMORY_TARGET_PCT = 70

# Never shrink an entry below this when consolidating. Emptying an entry would
# be a delete; the drain is not allowed to delete durable facts.
_MIN_CONSOLIDATED_ENTRY_CHARS = 32



PIN_MARKER = "[pinned]"


def is_pinned(entry: str) -> bool:
    """Return whether an entry is explicitly protected from projection rewrite."""
    return entry.strip().lower().startswith(PIN_MARKER)


def target_char_budget(limit: int) -> int:
    """Hysteresis low-water mark in characters for a store of *limit* chars."""
    if limit <= 0:
        return 0
    return max(0, (limit * MEMORY_TARGET_PCT) // 100)


def entries_char_total(entries: List[str]) -> int:
    """Serialized character count of *entries*, matching on-disk layout."""
    return len(ENTRY_DELIMITER.join(entries)) if entries else 0


def _summarize_entry_text(text: str, max_chars: int) -> str:
    """In-place shortening of one entry. Never returns empty for non-empty input."""
    text = text.strip()
    if max_chars <= 0:
        return text[:1] if text else ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return text[:1]
    return text[: max_chars - 1].rstrip() + "…"


def _near_duplicate_pair(left: str, right: str) -> bool:
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    window = min(48, len(left), len(right))
    return window >= 16 and left[:window].lower() == right[:window].lower()


def _merge_near_duplicate(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if left in right:
        return right
    if right in left:
        return left
    return left if len(left) >= len(right) else right


def _durable_facts_preserved(
    before: List[str], after: List[str], protected: Optional[set] = None,
) -> bool:
    """True when every pinned / protected entry from *before* is still in *after*."""
    after_set = set(after)
    protected = set(protected or ())
    for idx, text in enumerate(before):
        if idx in protected or is_pinned(text):
            if text not in after_set:
                return False
    return True


def consolidate_entries(
    entries: List[str],
    *,
    limit: int,
    protected: Optional[set] = None,
    target_chars: Optional[int] = None,
) -> List[str]:
    """Free room by merging/summarizing oldest CONTENT. Never deletes an entry.

    Pinned entries and indices in *protected* are left untouched. Entry count
    decreases only when two near-duplicate CONTENT entries are merged into one
    combined fact. Used by the drain when an LLM consolidator is unavailable
    or its result is rejected.
    """
    working = list(entries)
    protected_idx = set(protected or ())
    if target_chars is None:
        target_chars = target_char_budget(limit)

    def total() -> int:
        return entries_char_total(working)

    changed = True
    while changed and total() > target_chars:
        changed = False
        i = 0
        while i < len(working):
            if i in protected_idx or is_pinned(working[i]):
                i += 1
                continue
            j = i + 1
            while j < len(working):
                if j in protected_idx or is_pinned(working[j]):
                    j += 1
                    continue
                if _near_duplicate_pair(working[i], working[j]):
                    merged = _merge_near_duplicate(working[i], working[j])
                    trial = working[:i] + [merged] + working[i + 1 : j] + working[j + 1 :]
                    if entries_char_total(trial) <= total():
                        working = trial
                        protected_idx = {
                            (p - 1 if p > j else p) for p in protected_idx if p != j
                        }
                        changed = True
                        break
                j += 1
            if changed:
                break
            i += 1

    while total() > target_chars:
        candidates = [
            idx
            for idx, text in enumerate(working)
            if idx not in protected_idx
            and not is_pinned(text)
            and len(text.strip()) > _MIN_CONSOLIDATED_ENTRY_CHARS
        ]
        if not candidates:
            break
        idx = candidates[0]
        excess = total() - target_chars
        new_len = max(_MIN_CONSOLIDATED_ENTRY_CHARS, len(working[idx]) - excess)
        if new_len >= len(working[idx]):
            break
        working[idx] = _summarize_entry_text(working[idx], new_len)

    return working



# Sentinel: the target file EXISTS but could not be read (see ``_reload_target``).
_READ_FAILED = object()

# The operation the public entry point is currently running, so the ``_mutate``
# funnel can journal it when upstream's validation reports an over-limit failure.
# ContextVar => safe across threads / concurrent tool calls on one store.
_GUARD_OP: "contextvars.ContextVar[Optional[tuple]]" = contextvars.ContextVar(
    "memory_capacity_guard_op", default=None)


def _memory_dir():
    """Resolved lazily: tests monkeypatch ``tools.memory_tool.get_memory_dir``."""
    from tools import memory_tool
    return memory_tool.get_memory_dir()


class GuardedMemoryStore(_UpstreamMemoryStore):
    """Upstream ``MemoryStore`` plus the capacity guard (journal / overlay / projection).

    Drop-in replacement: ``tools.memory_tool.MemoryStore`` is rebound to this class
    by the ``FORK(memory-capacity-guard)`` hook in ``tools/memory_tool.py``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Entry lists captured at load time (sanitized). Used so a pending
        # overlay can be merged into injection without pulling mid-session
        # landed writes into the cached prefix.
        self._snapshot_entries: Dict[str, List[str]] = {"memory": [], "user": []}
        # Optional drain hook: (entries, target, limit, protected, target_chars,
        # queued_operations=None) -> list[str] | None. The journal consumer
        # installs an LLM consolidator here. apply_with_capacity falls back to
        # deterministic merge/summarize when this is unset or rejects.
        self.capacity_consolidator = None

    # -- load / snapshot ---------------------------------------------------

    def load_from_disk(self):
        super().load_from_disk()
        self._snapshot_entries = {
            "memory": self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md", log=False),
            "user": self._sanitize_entries_for_snapshot(self.user_entries, "USER.md", log=False),
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: List[str], filename: str, *, log: bool = True) -> List[str]:
        """Return ``entries`` with any threat-matching entry replaced by the same
        ``[BLOCKED: ...]`` placeholder upstream's ``load_from_disk`` puts in the
        snapshot (strict scope). Empty / already-blocked entries pass through."""
        from tools.threat_patterns import scan_for_threats

        sanitized: List[str] = []
        for entry in entries:
            findings = (scan_for_threats(entry, scope="strict")
                        if entry and not entry.startswith("[BLOCKED:") else None)
            if not findings:
                sanitized.append(entry)
                continue
            if log:
                logger.warning("Memory entry from %s blocked at load time: %s", filename, ", ".join(findings))
            sanitized.append(
                f"[BLOCKED: {filename} entry contained threat pattern(s): "
                f"{', '.join(findings)}. Removed from system prompt; "
                f"use memory(action=remove) "
                f"to delete the original.]"
            )
        return sanitized

    def _reload_target(self, target: str, *, skip_drift: bool = False):
        """Re-read entries from disk into in-memory state (called under file lock).

        Returns the drift backup path if external drift was detected, ``None`` on a
        clean reload, or ``_READ_FAILED`` when the file exists but could not be read
        (the caller MUST abort -- overwriting from an assumed-empty view would wipe it).
        """
        path = self._path_for(target)
        raw, read_ok = self._read_raw_checked(path)
        if not read_ok:
            return _READ_FAILED
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        self._set_entries(target, list(dict.fromkeys(self._parse_entries(raw))))
        return bak

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file."""
        self._path_for(target).parent.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    # -- usage / journal gate ------------------------------------------------

    def _usage_pct(self, target: str, current: Optional[int] = None):
        """Two shapes. ``_usage_pct(target)`` -> integer percentage clamped to [0,100]
        (fork API used by the guard and its tests). ``_usage_pct(target, current)`` ->
        upstream's ``"N% -- a/b chars"`` string (used by upstream's response/render code)."""
        if current is not None:
            return super()._usage_pct(target, current)
        current = self._char_count(target)
        limit = self._char_limit(target)
        if limit <= 0:
            return 100
        return min(100, int((current / limit) * 100))


    def _should_journal_capacity_write(self, target: str, usage_pct: int) -> bool:
        """Hysteresis gate for overflow journaling.

        High-water: usage >= QUEUE_PCT (85%) enters journal mode.
        Low-water: usage <= TARGET_PCT (70%) exits it.
        In the band (TARGET, QUEUE), stay journaling while overflow work is
        still pending so drain/apply cannot flap at the hard wall.
        """
        if usage_pct >= MEMORY_QUEUE_PCT:
            return True
        if usage_pct > MEMORY_TARGET_PCT and self._list_pending_for_target(target):
            return True
        return False

    def _journal_capacity_write(
        self, target: str, action: str, payload: Dict[str, Any],
        current: int, limit: int,
    ) -> Dict[str, Any]:
        """Durably journal a capacity-increasing write to the pending queue.

        Returns a ``queued``-shaped result so the agent knows the write is
        accepted but not yet applied — it must not retry.
        """
        from tools import memory_pending_queue as pq
        try:
            # Overflow writes are content-idempotent (same as add). Do not
            # stamp expected_previous_hash: a sibling queued add applied
            # first changes the snapshot hash and would dead-letter a
            # legitimate replace/remove. External drift is detected at
            # apply time only when no sibling overflow write exists.
            record = pq.enqueue(
                kind=pq.KIND_OVERFLOW,
                action=action,
                target=target,
                payload=payload,
                summary=f"{action} to {target} (overflow)",
                origin="overflow",
            )
        except Exception as e:
            # Try fallback journal if SQLite is unavailable
            record = self._journal_fallback(
                action, target, payload, current, limit, str(e)
            )
            if (
                record.get("success") is False
                or record.get("accepted") is False
                or record.get("reason") == "fallback_unavailable"
            ):
                return {
                    "success": False,
                    "accepted": False,
                    "reason": "fallback_unavailable",
                    "error": record.get("error") or (
                        "Memory write was not accepted; queue and fallback "
                        "journal are both unavailable."
                    ),
                    "queued": False,
                    "target": target,
                    "usage": f"{self._usage_pct(target)}% — {current:,}/{limit:,} chars",
                    "entry_count": len(self._entries_for(target)),
                }

        return {
            "success": True,
            "queued": True,
            "done": True,
            "message": (
                f"Memory {target} is at {current:,}/{limit:,} chars. "
                f"This write has been durably accepted and queued for "
                f"application (id={record.get('id', 'unknown')}). "
                f"It will be applied automatically by the projection consumer "
                f"or via /memory approve. Do not retry this write."
            ),
            "pending_id": record.get("id"),
            "target": target,
            "usage": f"{self._usage_pct(target)}% — {current:,}/{limit:,} chars",
            "entry_count": len(self._entries_for(target)),
        }

    def _journal_fallback(
        self, action: str, target: str, payload: Dict[str, Any],
        current: int, limit: int, error: str,
    ) -> Dict[str, Any]:
        """Fallback journal to a JSONL file when the SQLite queue is unavailable."""
        from pathlib import Path
        import json as _json

        fallback_path = _memory_dir() / "pending_fallback.jsonl"
        try:
            record = {
                "id": f"fallback-{int(time.time())}-{hash(action + target + str(payload)) & 0xFFFFFFFF:08x}",
                "kind": "overflow",
                "action": action,
                "target": target,
                "payload": payload,
                "queued_at": time.time(),
                "queue_error": error,
            }
            line = _json.dumps(record, ensure_ascii=False) + "\n"
            with open(fallback_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                import os
                os.fsync(f.fileno())
            logger.warning("memory_tool: fallback-journaled %s to %s (queue error: %s)",
                           action, fallback_path, error[:120])
            return record
        except Exception as fallback_err:
            logger.exception("memory_tool: fallback journal also failed")
            return {
                "success": False,
                "accepted": False,
                "reason": "fallback_unavailable",
                "error": (
                    f"Failed to accept memory write: SQLite queue ({error[:120]}) "
                    f"and fallback journal ({fallback_err}) both unavailable."
                ),
            }

    def _fallback_pending_for_target(self, target: str) -> List[Dict[str, Any]]:
        """Accepted overflow records from the JSONL fallback journal."""
        import json as _json

        path = _memory_dir() / "pending_fallback.jsonl"
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("target") == target:
                        out.append(rec)
        except OSError:
            return out
        return out

    def _list_pending_for_target(self, target: str) -> List[Dict[str, Any]]:
        """Return accepted/queued overflow records for *target*, oldest first."""
        records: List[Dict[str, Any]] = []
        try:
            from tools import memory_pending_queue as pq
            records.extend(
                r for r in pq.list_active(kind=pq.KIND_OVERFLOW)
                if r.get("target") == target
            )
        except Exception:
            pass
        records.extend(self._fallback_pending_for_target(target))
        records.sort(key=lambda r: r.get("created_at") or r.get("queued_at") or 0)
        return records

    @staticmethod
    def _apply_overlay_op(working: List[str], op: Optional[Dict[str, Any]]) -> List[str]:
        """Apply one queued op to a copy of entries. Skip unmatchable ops."""
        op = op or {}
        act = op.get("action")
        content = (op.get("content") or "").strip()
        old_text = (op.get("old_text") or "").strip()
        if act == "add":
            if content and content not in working:
                working.append(content)
        elif act == "replace":
            if old_text and content:
                matches = [j for j, e in enumerate(working) if old_text in e]
                if len({working[j] for j in matches}) == 1:
                    working[matches[0]] = content
        elif act == "remove":
            if old_text:
                matches = [j for j, e in enumerate(working) if old_text in e]
                if len({working[j] for j in matches}) == 1:
                    working.pop(matches[0])
        return working

    def _apply_pending_overlay(
        self, entries: List[str], records: List[Dict[str, Any]],
    ) -> List[str]:
        """Replay queued overflow records onto *entries* in enqueue order."""
        working = list(entries)
        for rec in records:
            action = rec.get("action")
            payload = rec.get("payload") or {}
            if action == "batch":
                ops = payload.get("operations") or []
            else:
                ops = [{
                    "action": action,
                    "content": payload.get("content"),
                    "old_text": payload.get("old_text"),
                }]
            for op in ops:
                working = self._apply_overlay_op(working, op)
        return working

    def entries_for_read(self, target: str) -> List[str]:
        """Live on-disk entries plus accepted/queued overflow writes."""
        return self._apply_pending_overlay(
            list(self._entries_for(target)),
            self._list_pending_for_target(target),
        )


    # -- operation interception ---------------------------------------------

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Journals to the pending queue when at capacity."""
        token = _GUARD_OP.set(("add", {"content": content.strip()}))
        try:
            return super().add(target, content)
        finally:
            _GUARD_OP.reset(token)

    def _edit(self, target: str, old_text: str, new_content: Optional[str]) -> Dict[str, Any]:
        """Replace/remove. A capacity-increasing replace journals when at capacity;
        remove (``new_content is None``) is never journaled."""
        op = None if new_content is None else ("replace", {"old_text": old_text, "content": new_content})
        token = _GUARD_OP.set(op)
        try:
            return super()._edit(target, old_text, new_content)
        finally:
            _GUARD_OP.reset(token)

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Atomic batch. An over-limit batch journals (whole) when at capacity."""
        token = _GUARD_OP.set(("batch", {"operations": operations}))
        try:
            return super().apply_batch(target, operations)
        finally:
            _GUARD_OP.reset(token)

    def _would_overflow(self, action: str, payload: Dict[str, Any], entries: List[str], limit: int) -> bool:
        """True when *action* is otherwise VALID and its result exceeds *limit*.

        Replays the operation on a copy with upstream's own ``_apply_batch_op`` so
        malformed / unmatched / ambiguous ops (which must keep failing normally) are
        never mistaken for capacity overflow. A replace only counts when it grows
        the entry (capacity-increasing).
        """
        from tools.memory_tool_store import _find_unique_match

        working = list(entries)
        if action == "add":
            ops = [{"action": "add", "content": payload["content"]}]
        elif action == "replace":
            idx, _ambiguous = _find_unique_match(entries, payload["old_text"])
            if idx is None or len(payload["content"]) <= len(entries[idx]):
                return False
            ops = [{"action": "replace", "old_text": payload["old_text"], "content": payload["content"]}]
        else:
            ops = [op or {} for op in payload["operations"]]
        for i, op in enumerate(ops):
            act = op.get("action")
            if self._apply_batch_op(working, act, (op.get("content") or op.get("new_text") or "").strip(),
                                    (op.get("old_text") or "").strip(), f"Operation {i + 1}"):
                return False
        return bool(working) and len(ENTRY_DELIMITER.join(working)) > limit

    def _mutate(self, target: str, mutate, *, skip_drift: bool = False) -> Dict[str, Any]:
        """Upstream's single locked read-modify-write funnel, with the guard wrapped
        around the caller's ``mutate`` callback (so it runs inside the file lock, on
        the freshly reloaded entries, exactly where the fork's inline code used to)."""
        op = _GUARD_OP.get()
        if op is None:
            return super()._mutate(target, mutate, skip_drift=skip_drift)
        action, payload = op

        def guarded(entries, limit):
            failures_before = self._consolidation_failures
            result = mutate(entries, limit)
            if not isinstance(result, dict) or result.get("success") is not False:
                return result
            if self._would_overflow(action, payload, entries, limit) and self._should_journal_capacity_write(
                    target, self._usage_pct(target)):
                # Journaled, not a failed consolidation attempt: undo upstream's counter bump.
                self._consolidation_failures = failures_before
                return self._journal_capacity_write(
                    target, action, payload, self._char_count(target), limit)
            if "current_entries" in result:
                # Show the model its own accepted-but-queued writes too.
                result["current_entries"] = self.entries_for_read(target)
            return result

        return super()._mutate(target, guarded, skip_drift=skip_drift)


    def apply_with_capacity(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply add/replace/remove operations without deleting durable entries.

        This is the projection consumer's entry point (see
        ``tools/memory_projection.py``) for replaying a journal record that
        already survived the normal write gate/threat scan once (at enqueue
        time) -- it must not go through :meth:`add`/:meth:`replace` and their
        auto-queue-on-overflow behavior, or applying a queued record would
        just enqueue a duplicate of itself. Callers other than the projection
        consumer should use :meth:`add`/:meth:`replace`/:meth:`remove`/
        :meth:`apply_batch` instead.

        Validation and content scanning mirror :meth:`apply_batch` exactly
        (same op semantics, same all-or-nothing intent for malformed input).
        When the projected store is above TARGET_PCT (or over the hard
        limit), this consolidates oldest CONTENT -- merge near-duplicates,
        then summarize -- and never FIFO-deletes an entry. Pinned entries
        and entries this SAME call is adding/replacing are left intact.

        The journal consumer should install :attr:`capacity_consolidator`
        (LLM-guided) before drain; if that hook is missing or rejects, a
        deterministic consolidator runs instead.

        Returns the normal success/error shape plus ``"evicted": []`` (kept
        for caller compatibility; drain does not delete). If consolidation
        cannot bring the result under the hard limit, returns
        ``{"success": False, "unresolvable": True, ...}`` -- the caller must
        dead-letter the record and alert the operator.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if act in {"add", "replace"} and new_content:
                scan_error = _scan_memory_content(new_content)
                if scan_error:
                    return {"success": False, "error": f"Operation {i + 1}: {scan_error}"}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target)
            # Indices into `working` this call itself wrote -- never evicted.
            protected: set = set()

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    if content in working:
                        continue
                    working.append(content)
                    protected.add(len(working) - 1)

                elif act == "replace":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    if not content:
                        return self._batch_error(
                            target,
                            f"{pos}: content is required (use action='remove' to delete).",
                        )
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    idx = matches[0]
                    working[idx] = content
                    protected.add(idx)

                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    idx = matches[0]
                    working.pop(idx)
                    protected = {j - 1 if j > idx else j for j in protected if j != idx}

                else:
                    return self._batch_error(
                        target,
                        f"{pos}: unknown action. Use add, replace, or remove.",
                    )

            target_chars = target_char_budget(limit)
            if entries_char_total(working) > target_chars:
                consolidator = getattr(self, "capacity_consolidator", None)
                if callable(consolidator):
                    try:
                        candidate = consolidator(
                            working,
                            target,
                            limit,
                            protected,
                            target_chars,
                            queued_operations=operations,
                        )
                    except Exception:
                        logger.exception(
                            "memory_tool: capacity consolidator failed for %s",
                            target,
                        )
                        candidate = None
                    if (
                        isinstance(candidate, list)
                        and candidate
                        and _durable_facts_preserved(working, candidate, protected)
                        and entries_char_total(candidate) <= limit
                    ):
                        working = candidate
                if entries_char_total(working) > target_chars:
                    working = consolidate_entries(
                        working,
                        limit=limit,
                        protected=protected,
                        target_chars=target_chars,
                    )

            if entries_char_total(working) > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "unresolvable": True,
                    "error": (
                        f"Cannot make room in {target}: consolidation could not "
                        f"bring the store under the {limit:,}-char limit "
                        f"(result {entries_char_total(working):,} chars) without "
                        f"deleting durable entries. Unpin/shorten entries or raise "
                        f"the limit."
                    ),
                    "current_entries": self._entries_for(target),
                    "usage": f"{current:,}/{limit:,}",
                }

            self._set_entries(target, working)
            self.save_to_disk(target)

        resp = self._success_response(target, f"Applied {len(operations)} operation(s).")
        resp["evicted"] = []
        return resp

    def _batch_error(self, target: str, message: str) -> Dict[str, Any]:
        """Build a batch-abort error that reports live (uncommitted) state."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": self.entries_for_read(target),
            "usage": f"{current:,}/{limit:,}",
        })


    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """Return the snapshot for system prompt injection.

        Landed mid-session writes stay out of this block (prefix-cache invariant).
        Accepted/queued overflow records are merged on top of the load-time snapshot
        so this session can read its own queued writes. When the overflow queue is
        empty the returned string is the frozen load-time block, byte-stable.

        Returns None if the (possibly overlaid) snapshot is empty.
        """
        pending = self._list_pending_for_target(target)
        if not pending:
            return super().format_for_system_prompt(target)
        base = list(self._snapshot_entries.get(target, []))
        merged = self._apply_pending_overlay(base, pending)
        filename = "USER.md" if target == "user" else "MEMORY.md"
        sanitized = self._sanitize_entries_for_snapshot(merged, filename)
        block = self._render_block(target, sanitized)
        return block if block else None

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        resp = super()._success_response(target, message)
        # Capacity-awareness: surface queued count and warn when near the cap.
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = self._usage_pct(target)
        pending = self._list_pending_for_target(target)
        if pending:
            resp["pending_count"] = len(pending)
        if pct >= MEMORY_WARN_PCT:
            resp["capacity_note"] = (
                f"Memory {target} is at {pct}% ({current:,}/{limit:,} chars). "
                f"Consider consolidating soon — use an 'operations' batch with "
                f"replace/remove to shorten or remove stale entries."
            )
        return resp
