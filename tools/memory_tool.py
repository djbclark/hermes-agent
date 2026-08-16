#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations (environment facts, project
    conventions, tool quirks, things learned)
  - USER.md: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt -- this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Capacity guard (Phase A): when a write would exceed the store's character limit,
it is durably journaled to a SQLite pending-operation queue instead of being
rejected. The agent receives a ``queued`` result with the operation ID rather
than a ``success: false`` error, so it does not retry or lose the fact.

  - WARN at 75 %  -- usage note is appended to success responses but the write
                     still lands in the live store.
  - JOURNAL at 85 % -- capacity-increasing writes are durably queued instead of
                     applied directly; non-increasing writes (remove, shorter
                     replace) still land live.
  - PROJECT at ≤70 % -- the projection consumer's target after journaled entries
                     are applied. Drain consolidates (merge/summarize CONTENT);
                     it never deletes durable entries.
  - Hysteresis: journal mode latches at QUEUE_PCT and stays latched while
                usage is above TARGET_PCT and overflow work is still pending.

Design:
- Single `memory` tool with action parameter: add, replace, remove
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import atomic_write_text

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

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# Where memory files live — resolved dynamically so profile overrides
# (HERMES_HOME env var changes) are always respected.  The old module-level
# constant was cached at import time and could go stale if a profile switch
# happened after the first import.
def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"

# Stable header prefixes for the system-prompt memory blocks rendered by
# MemoryStore._render_block. Exported so compression's prompt-retention check
# (agent/conversation_compression.py) can detect a leftover block for a
# target whose entries have since been emptied — keep in lockstep with
# _render_block below.
MEMORY_BLOCK_HEADERS = {
    "memory": "MEMORY (your personal notes)",
    "user": "USER PROFILE (who the user is)",
}

ENTRY_DELIMITER = "\n§\n"


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


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
#
# Patterns live in ``tools/threat_patterns.py`` — the single source of truth
# shared with the context-file scanner and the tool-result delimiter system.
# Memory uses the "strict" scope (broadest pattern set) because:
#  - memory entries are user-curated; the user can rewrite a flagged entry
#  - memory enters the system prompt as a FROZEN snapshot, so a poisoned
#    entry persists for the entire session and across sessions until
#    explicitly removed.
# ---------------------------------------------------------------------------

from tools.threat_patterns import first_threat_message as _first_threat_message


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    return _first_threat_message(content, scope="strict")


def _drift_error(path: "Path", bak_path: str) -> Dict[str, Any]:
    """Build the error dict returned when external drift is detected.

    The on-disk memory file contains content that wouldn't round-trip
    through the tool's parser/serializer — flushing would discard the
    appended/edited content from a patch tool, shell append, manual edit,
    or sister-session write. We refuse the mutation, point the operator at
    the .bak.<ts> snapshot we took, and tell them what to do next.
    """
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: file on disk has content that "
            f"wouldn't round-trip through the memory tool (likely added by "
            f"the patch tool, a shell append, a manual edit, or a "
            f"concurrent session). A snapshot was saved to {bak_path}. "
            f"Resolve the drift first — either rewrite the file as a clean "
            f"§-delimited list of entries, or move the extra content out — "
            f"then retry. This guard exists to prevent silent data loss "
            f"(issue #26045)."
        ),
        "drift_backup": bak_path,
        "remediation": (
            "Open the .bak file, integrate the missing entries into the "
            "memory tool one at a time via memory(action=add, content=...), "
            "then remove or rewrite the original file to a clean state."
        ),
    }


# Sentinel returned by ``_reload_target`` when the target file EXISTS but could
# not be read. Distinct from a drift-backup path (``str``) and from a clean
# reload (``None``): the caller must abort the mutation rather than persist over
# an unreadable file.
_READ_FAILED = object()


def _read_failed_error(path: "Path") -> Dict[str, Any]:
    """Build the error dict returned when the on-disk memory file is unreadable.

    A file that exists but cannot be read is NOT an empty store. Reading it as
    ``[]`` and then persisting would rewrite the whole file from an empty entry
    list — wiping the user's memory. We refuse the write so nothing is lost.
    """
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: the file exists on disk but could "
            f"not be read right now (temporarily locked by another program, a "
            f"permission change, invalid/corrupt text encoding, or a filesystem "
            f"error). Treating an unreadable file as empty and saving would wipe "
            f"existing memory, so the write is refused. Nothing was changed — "
            f"retry in a moment."
        ),
    }


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    # After this many failed consolidation attempts (overflow / zero-match) in
    # ONE turn, stop instructing the model to "retry in this turn" and return a
    # terminal "save skipped" result so a fragile replace/add can't loop the
    # turn to budget exhaustion and suppress the user's reply (issue #42405).
    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
        # Entry lists captured at load time (sanitized). Used so a pending
        # overlay can be merged into injection without pulling mid-session
        # landed writes into the cached prefix.
        self._snapshot_entries: Dict[str, List[str]] = {"memory": [], "user": []}
        # Per-turn counter of failed at-capacity consolidation attempts; reset
        # at each turn boundary by reset_consolidation_failures() (#42405).
        self._consolidation_failures = 0
        # Optional drain hook: (entries, target, limit, protected, target_chars,
        # queued_operations=None) -> list[str] | None. The journal consumer
        # installs an LLM consolidator here. apply_with_capacity falls back to
        # deterministic merge/summarize when this is unset or rejects.
        self.capacity_consolidator = None

    def reset_consolidation_failures(self) -> None:
        """Reset the per-turn consolidation-failure counter (call at turn start)."""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count an at-capacity consolidation failure and degrade gracefully.

        Under the per-turn cap, return ``response`` unchanged (it already tells
        the model how to self-correct + retry in this turn). Once the cap is
        exceeded, drop the retry instruction and return a TERMINAL result so the
        model stops looping memory calls and proceeds to answer the user — a
        failed memory side effect must never block the turn's reply (#42405).
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times "
                "this turn. Stop retrying memory calls — leave memory unchanged for "
                "now and continue with your reply to the user. The fact can be saved "
                "in a later turn."
            ),
        }

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot.

        The frozen snapshot is what enters the system prompt. We scan each
        entry for injection/promptware patterns at snapshot-build time —
        ANY hit replaces the entry text in the snapshot with a placeholder
        like ``[BLOCKED: …]``, so a poisoned-on-disk memory file (supply
        chain, compromised tool, sister-session write) cannot inject into
        the system prompt.

        The live ``memory_entries`` / ``user_entries`` lists keep the
        original text so the user can still SEE poisoned entries via
        see poisoned entries by inspecting the source files directly, and remove them — silently dropping them would hide the attack from the user.

        Scanning is deterministic from disk bytes, so the snapshot remains
        stable for the entire session (prefix-cache invariant holds).
        """
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Sanitize entries for the system-prompt snapshot only.  Live state
        # (memory_entries / user_entries) keeps the raw text so the user
        # can see + remove poisoned entries via the memory tool.
        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")

        # Capture frozen snapshot for system prompt injection
        self._snapshot_entries = {
            "memory": list(sanitized_memory),
            "user": list(sanitized_user),
        }
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: List[str], filename: str) -> List[str]:
        """Return ``entries`` with any threat-matching entry replaced by a placeholder.

        Each entry is scanned with the shared threat-pattern library at the
        ``"strict"`` scope (same as memory writes).  On match, the entry is
        replaced in the returned list with ``"[BLOCKED: <filename> entry
        contained threat pattern: <ids>. Removed from system prompt.]"`` —
        the placeholder enters the snapshot, the original entry stays in
        live state for the user to inspect and delete.

        Empty or already-block-marker entries pass through unchanged.
        """
        from tools.threat_patterns import scan_for_threats

        sanitized: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                logger.warning(
                    "Memory entry from %s blocked at load time: %s",
                    filename, ", ".join(findings),
                )
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str, *, skip_drift: bool = False):
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        Returns the backup path if external drift was detected (the on-disk
        file contains content that wouldn't round-trip through our
        parser/serializer, OR an entry larger than the store's char limit).
        When drift is detected the caller must abort the mutation —
        flushing would discard the un-roundtrippable content.
        Returns ``None`` on clean reload.

        Returns the ``_READ_FAILED`` sentinel when the file EXISTS but could not
        be read. The caller MUST abort: the on-disk entries are unknown, so
        overwriting from an assumed-empty view would wipe them. This is the real
        exposure behind ``add`` — it skips the drift guard because appending is
        safe, but that reasoning only holds when the reload actually saw the
        file. A failed read reported as ``[]`` turned ``add`` into a full-file
        rewrite down to a single entry.

        When *skip_drift* is True the round-trip / entry-size check is
        bypassed.  Used by the ``add`` action which appends without
        rewriting, so existing content is never clobbered.
        """
        path = self._path_for(target)
        raw, read_ok = self._read_raw_checked(path)
        if not read_ok:
            # Leave in-memory entries untouched and tell the caller to abort;
            # persisting over an unreadable file would destroy it.
            return _READ_FAILED
        # Derive BOTH the drift check and the entry parse from the same raw
        # snapshot. The drift guard used to re-read the file itself and treat
        # a failed second read as "no drift" — so a read failure between the
        # checked reload and the drift check let replace/remove/apply_batch
        # rewrite the file from a stale view, silently discarding whatever an
        # external writer had just added. One read, one snapshot, no window.
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        fresh = self._parse_entries(raw)
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def _usage_pct(self, target: str) -> int:
        """Current usage as an integer percentage, clamped to [0,100]."""
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

        fallback_path = get_memory_dir() / "pending_fallback.jsonl"
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

        path = get_memory_dir() / "pending_fallback.jsonl"
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

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Journals to the pending queue when at capacity."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            if self._reload_target(target, skip_drift=True) is _READ_FAILED:
                return _read_failed_error(self._path_for(target))

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # Calculate what the new total would be
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))
            current = self._char_count(target)
            usage_pct = self._usage_pct(target)

            if new_total > limit:
                if self._should_journal_capacity_write(target, usage_pct):
                    return self._journal_capacity_write(
                        target, "add", {"content": content},
                        current, limit,
                    )
                # Below queue threshold — still reject with consolidation
                # guidance, but don't journal (the model can consolidate in-turn).
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries into "
                        f"shorter ones or 'remove' stale or less important entries (see "
                        f"current_entries below), then retry this add — all in this turn."
                    ),
                    "current_entries": self.entries_for_read(target),
                    "usage": f"{current:,}/{limit:,}",
                })

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content.

        Journals to the pending queue when the replacement would exceed the
        char limit and the store is at or above the queue threshold."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to replace.",
                    "current_entries": self.entries_for_read(target),
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews([e for _, e in matches])
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            limit = self._char_limit(target)
            current = self._char_count(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))
            old_entry_len = len(entries[idx])
            new_entry_len = len(new_content)

            if new_total > limit:
                usage_pct = self._usage_pct(target)
                # If the store is at the queue threshold AND this is a
                # capacity-increasing replace (new > old), journal it.
                if (
                    self._should_journal_capacity_write(target, usage_pct)
                    and new_entry_len > old_entry_len
                ):
                    return self._journal_capacity_write(
                        target, "replace",
                        {"old_text": old_text, "content": new_content},
                        current, limit,
                    )
                # Below queue threshold or non-increasing — reject with guidance.
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other stale or less important "
                        f"entries to make room (see current_entries below), then retry — all "
                        f"in this turn."
                    ),
                    "current_entries": self.entries_for_read(target),
                    "usage": f"{current:,}/{limit:,}",
                })

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to remove.",
                    "current_entries": self.entries_for_read(target),
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews([e for _, e in matches])
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply a sequence of add/replace/remove ops to one target atomically.

        All operations are validated and applied against the FINAL budget --
        intermediate overflow is irrelevant. This lets the model free space
        (remove/replace) and add new entries in a SINGLE tool call instead of
        the multi-turn consolidate-then-retry dance that re-sends the whole
        conversation context several times.

        Semantics: all-or-nothing. If any op is malformed, doesn't match, or
        the net result would exceed the char limit, NOTHING is written and an
        error is returned describing the first failure plus the live state.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        # Scan every add/replace content for injection/exfil BEFORE touching
        # disk -- a single poisoned op rejects the whole batch.
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

            # Work on a copy; only commit if the whole batch validates.
            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target)

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
                        continue  # idempotent -- skip duplicate, don't fail the batch
                    working.append(content)

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
                    working[matches[0]] = content

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
                    working.pop(matches[0])

                else:
                    return self._batch_error(
                        target,
                        f"{pos}: unknown action. Use add, replace, or remove.",
                    )

            # Budget check against the FINAL state only.
            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                usage_pct = self._usage_pct(target)
                # If at the queue threshold, journal the entire batch rather
                # than rejecting — the model already tried to consolidate.
                if self._should_journal_capacity_write(target, usage_pct):
                    return self._journal_capacity_write(
                        target, "batch",
                        {"operations": operations},
                        current, limit,
                    )
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"After applying all {len(operations)} operations, memory would be at "
                        f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                        f"entries in the same batch (see current_entries below), then retry."
                    ),
                    "current_entries": self.entries_for_read(target),
                    "usage": f"{current:,}/{limit:,}",
                })

            # Commit.
            self._set_entries(target, working)
            self.save_to_disk(target)

        return self._success_response(target, f"Applied {len(operations)} operation(s).")

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
        """
        Return the snapshot for system prompt injection.

        Landed mid-session writes stay out of this block (prefix-cache
        invariant). Accepted/queued overflow records are merged on top of
        the load-time snapshot so this session can read its own queued
        writes. When the overflow queue is empty the returned string is
        the frozen load-time block, byte-stable.

        Returns None if the (possibly overlaid) snapshot is empty.
        """
        pending = self._list_pending_for_target(target)
        if not pending:
            block = self._system_prompt_snapshot.get(target, "")
            return block if block else None
        base = list(self._snapshot_entries.get(target, []))
        merged = self._apply_pending_overlay(base, pending)
        filename = "USER.md" if target == "user" else "MEMORY.md"
        sanitized = self._sanitize_entries_for_snapshot(merged, filename)
        block = self._render_block(target, sanitized)
        return block if block else None

    # -- Internal helpers --

    @staticmethod
    def _previews(entries: List[str], width: int = 80) -> List[str]:
        """Truncated one-line previews of entries for error feedback."""
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        # A successful write means the consolidation loop made progress, so the
        # per-turn failure budget resets (the cap counts consecutive failures,
        # not lifetime ones within a turn) (#42405).
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = self._usage_pct(target)

        # The success response is intentionally TERMINAL: it confirms the write
        # landed and tells the model to stop. We do NOT echo the full entries
        # list here -- dumping it invites the model to "find more to fix" and
        # re-issue the same operations (observed thrash: the correct batch on
        # call 1, then 5 redundant repeats). Entries are only shown on the
        # error/over-budget paths, where the model genuinely needs them to
        # decide what to consolidate.
        resp = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."

        # Capacity-awareness: surface queued count and warn when near the cap.
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

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"{MEMORY_BLOCK_HEADERS['user']} [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"{MEMORY_BLOCK_HEADERS['memory']} [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_raw_checked(path: Path) -> Tuple[str, bool]:
        """Read a memory file's raw text, distinguishing unreadable from empty.

        Returns ``(raw, read_ok)``. ``read_ok`` is False ONLY when the file
        EXISTS but could not be read — an absent file is a clean ``("", True)``.
        Invalid UTF-8 counts as unreadable too: the bytes on disk hold content
        we cannot faithfully round-trip, so a rewrite would corrupt or discard
        it just like a failed read. Read-modify-write callers must treat
        ``read_ok=False`` as "abort" rather than "empty store", or a transient
        read failure would let them persist over — and wipe — the on-disk
        memory (issue #26045 is about the same class: never rewrite a file
        from a view that isn't the real one).

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return "", True
        try:
            # utf-8-sig strips a leading UTF-8 BOM (Notepad-edited memory
            # files on Windows) and is byte-identical to utf-8 otherwise.
            # Plain utf-8 kept U+FEFF glued to the first entry, corrupting
            # matching/dedup for that entry forever (#10878 / PR #10888).
            # Decode errors stay STRICT on purpose: errors="replace" would
            # hand read-modify-write callers a lossy view that a subsequent
            # save persists over the real bytes — the wipe class documented
            # above. Undecodable bytes must surface as read_ok=False.
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, IOError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> List[str]:
        """Split raw memory-file text into stripped, non-empty entries."""
        if not raw.strip():
            return []
        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _read_entries_checked(path: Path) -> Tuple[List[str], bool]:
        """Read + parse a memory file, distinguishing unreadable from empty.

        Returns ``(entries, read_ok)`` — see ``_read_raw_checked`` for the
        ``read_ok`` contract.
        """
        raw, read_ok = MemoryStore._read_raw_checked(path)
        if not read_ok:
            return [], False
        return MemoryStore._parse_entries(raw), True

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries (empty list on any error).

        Retained for read-only callers (``load_from_disk``) that build in-memory
        state without persisting; a failed read degrading to ``[]`` there is
        harmless because nothing is written back. Read-modify-write paths use
        ``_read_raw_checked`` so they can refuse to overwrite an unreadable
        file — see ``_reload_target``.
        """
        return MemoryStore._read_entries_checked(path)[0]

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        """Return a backup-path string if on-disk content shows external drift.

        *raw* is the file content already read by the caller's checked read
        (``_read_raw_checked``). Drift detection MUST operate on that same
        snapshot — an earlier version re-read the file here and treated a
        failed second read as "no drift", which let a mutation proceed from a
        stale first snapshot and rewrite away content an external writer added
        between the two reads.

        The memory file is supposed to be a list of small entries the tool
        wrote, joined by §. Detect drift via two signals:

        1. Round-trip mismatch — re-parsing and re-serializing the file
           doesn't produce identical bytes (rare; would catch oddly-encoded
           delimiters).
        2. Entry-size overflow — any single parsed entry exceeds the
           store's whole-file char limit. The tool budgets the ENTIRE store
           against that limit; no single tool-written entry can exceed it.
           When we see one entry larger than the limit, an external writer
           (patch tool, shell append, manual edit, sister session) appended
           free-form content into what the tool will treat as one entry.
           Flushing would then truncate that entry to the model's new
           content, discarding the appended bytes — issue #26045.

        Returns the absolute path of the .bak file when drift was found and
        backed up; returns None when the file looks tool-shaped.

        Note: this is an INSTANCE method (not static) because we need the
        per-target char_limit for signal #2.
        """
        path = self._path_for(target)
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        # Drift confirmed — snapshot the file so the operator can recover
        # whatever the external writer added, then return the .bak path so
        # the caller can refuse the mutation.
        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            atomic_write_text(path, content, tmp_prefix=".mem_")
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def load_on_disk_store() -> "MemoryStore":
    """Build a fresh on-disk :class:`MemoryStore`, honoring configured char limits.

    Use this from any context that has no live agent (the messaging gateway, the
    Desktop GUI, the bare CLI ``/memory`` handler) but still needs to read or
    apply approved memory writes. Mirrors how the live agent constructs its store
    in ``agent/agent_init.py`` — including the user's ``memory.memory_char_limit``
    / ``memory.user_char_limit`` overrides — so an approval applied without a live
    agent enforces the SAME caps as one applied with one.

    Falls back to the built-in defaults if config can't be loaded, so this can
    never raise on a missing/unreadable config.
    """
    memory_char_limit = 2200
    user_char_limit = 1375
    try:
        from hermes_cli.config import load_config

        mem_cfg = (load_config() or {}).get("memory", {}) or {}
        memory_char_limit = int(mem_cfg.get("memory_char_limit", memory_char_limit))
        user_char_limit = int(mem_cfg.get("user_char_limit", user_char_limit))
    except Exception:
        pass  # config optional — fall back to defaults rather than break /memory

    store = MemoryStore(
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
    )
    store.load_from_disk()
    return store


def _apply_write_gate(action: str, target: str, content: Optional[str],
                      old_text: Optional[str]) -> Optional[str]:
    """Evaluate the memory write gate. Returns a JSON tool-result string when
    the write should NOT proceed normally (blocked or staged), or None when the
    caller should perform the real write.

    Only the mutating actions (add/replace/remove) are gated.
    """
    if action not in {"add", "replace", "remove"}:
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        # If the gate module can't load, fail open (current behaviour) rather
        # than blocking all memory writes.
        return None

    # Build a small inline summary/detail for the foreground approval prompt.
    label = "user profile" if target == "user" else "memory"
    if action == "add":
        summary = f"add to {label}"
        detail = content or ""
    elif action == "replace":
        summary = f"replace in {label}"
        detail = f"old: {old_text}\nnew: {content}"
    else:  # remove
        summary = f"remove from {label}"
        detail = old_text or ""

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage
    payload = {
        "action": action,
        "target": target,
        "content": content,
        "old_text": old_text,
    }
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _apply_batch_write_gate(target: str, operations: List[Dict[str, Any]]) -> Optional[str]:
    """Evaluate the write gate for a batch of memory operations.

    Returns a JSON tool-result string when the batch should NOT proceed
    (blocked or staged), or None when the caller should perform the real
    batch write. The whole batch is gated as a single unit.
    """
    try:
        from tools import write_approval as wa
    except Exception:
        return None

    label = "user profile" if target == "user" else "memory"
    summary = f"apply {len(operations)} op(s) to {label}"
    detail_lines = []
    for op in operations:
        op = op or {}
        act = op.get("action", "?")
        if act == "remove":
            detail_lines.append(f"- remove: {op.get('old_text', '')}")
        elif act == "replace":
            detail_lines.append(f"- replace: {op.get('old_text', '')} -> {op.get('content', '')}")
        else:
            detail_lines.append(f"- {act}: {op.get('content', '')}")
    detail = "\n".join(detail_lines)

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    payload = {"action": "batch", "target": target, "operations": operations}
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _missing_old_text_error(store: "MemoryStore", target: str, action: str) -> str:
    """Build a recoverable error for a replace/remove call that arrived without
    ``old_text``.

    ``replace``/``remove`` are inherently targeted -- without ``old_text`` there
    is no entry to act on, so we cannot fulfil the call. But returning a bare
    "old_text is required" is a dead-end: some structured-output clients omit the
    optional ``old_text`` field (it isn't, and can't be, schema-required without
    a top-level combinator the Codex backend rejects -- see
    tests/tools/test_memory_tool_schema.py). So instead we return the current
    entry inventory plus an explicit retry instruction, letting the model reissue
    the call with ``old_text`` set to a unique substring of the entry it means.
    Mirrors the batch path's ``_batch_error`` shape. (issues #43412, #49466)
    """
    entries = store.entries_for_read(target)
    current = store._char_count(target)
    limit = store._char_limit(target)
    return json.dumps(
        {
            "success": False,
            "error": (
                f"'{action}' needs old_text -- a short unique substring of the entry "
                f"to {action}. None was provided. Reissue the {action} with old_text "
                f"set to part of one of the current_entries below."
            ),
            "current_entries": entries,
            "usage": f"{current:,}/{limit:,}",
        },
        ensure_ascii=False,
    )


def memory_tool(
    action: str = None,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    operations: Optional[List[Dict[str, Any]]] = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Two shapes:
      - Single op: action + (content / old_text).
      - Batch:     operations=[{action, content?, old_text?}, ...] applied
                   atomically against the final char budget in ONE call.

    Returns JSON string with results.
    """
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    # Some strict providers fill optional schema fields with JSON null rather
    # than omitting them.  Treat ``target: null`` as omitted so memory writes
    # still use the documented default store instead of failing validation.
    if target is None:
        target = "memory"

    if target not in {"memory", "user"}:
        return tool_error(f"Invalid target '{target}'. Use 'memory' or 'user'.", success=False)

    # --- Batch path -------------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return tool_error("operations must be a list of {action, content?, old_text?} objects.", success=False)
        gate_result = _apply_batch_write_gate(target, operations)
        if gate_result is not None:
            return gate_result
        result = store.apply_batch(target, operations)
        return json.dumps(result, ensure_ascii=False)

    # --- Single-op path ---------------------------------------------------
    # Validate required params BEFORE the gate so an invalid write is rejected
    # immediately instead of being staged and only failing at approve time.
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action == "replace" and (not old_text or not content):
        missing = "old_text" if not old_text else "content"
        if not old_text:
            # The client/model omitted old_text. Replace is inherently targeted
            # -- we can't guess which entry. Return the current inventory plus a
            # retry instruction so the model can reissue with old_text set,
            # instead of hitting a dead-end error. (issues #43412, #49466)
            return _missing_old_text_error(store, target, "replace")
        return tool_error(f"{missing} is required for 'replace' action.", success=False)
    if action == "remove" and not old_text:
        return _missing_old_text_error(store, target, "remove")

    # Approval gate: when on, stages the write (background/gateway) or prompts
    # inline (interactive CLI); when off (default) passes straight through.
    gate_result = _apply_write_gate(action, target, content, old_text)
    if gate_result is not None:
        return gate_result

    if action == "add":
        result = store.add(target, content)

    elif action == "replace":
        result = store.replace(target, old_text, content)

    elif action == "remove":
        result = store.remove(target, old_text)

    else:
        return tool_error(f"Unknown action '{action}'. Use: add, replace, remove", success=False)

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged memory write directly against the store, bypassing the
    write gate. Called by the /memory approve handler.

    Returns the store's result dict.
    """
    action = payload.get("action")
    target = payload.get("target", "memory")
    content = payload.get("content") or ""
    old_text = payload.get("old_text") or ""
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action == "add":
        return store.add(target, content)
    if action == "replace":
        return store.replace(target, old_text, content)
    if action == "remove":
        return store.remove(target, old_text)
    return {"success": False, "error": f"Unknown staged action '{action}'."}
# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
        "single lone change.\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow. "
        "Priority: user preferences & corrections > environment facts > procedures. The best "
        "memory stops the user repeating themselves.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
        "removes or shortens enough stale entries and adds the new one together.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform (single-op shape). Omit when using 'operations'."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace' (single-op shape)."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring identifying the existing entry to modify. Omit only for 'add'."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple changes "
                    "or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string", "description": "Entry content for add/replace."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["target"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        operations=args.get("operations"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)




