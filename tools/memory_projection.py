#!/usr/bin/env python3
"""Deterministic, non-LLM journal consumer/projection layer for memory.

``tools/memory_pending_queue.py`` durably journals memory writes that either
overflowed the bounded MEMORY.md/USER.md store or were staged for approval,
but (per its own module docstring) provides "durable journal + review
storage only" -- resolution previously required a human running
``/memory pending`` / ``/memory approve``. This module is the automated
consumer that phase adds: it claims journal records under a lease, applies
them to the bounded store (the "projection"), and resolves them --
idempotently, crash-safely, and without ever calling an LLM.

Lifecycle per record (mirrors ``memory_pending_queue``'s own state machine):

  1. :func:`claim_and_apply` claims the oldest available record via
     ``pq.claim_next`` (this atomically covers fresh ``pending`` rows, rows
     whose retry delay has elapsed, AND rows whose lease expired after a
     crashed/killed consumer -- see that function's docstring).
  2. The record is applied against a fresh on-disk :class:`MemoryStore`
     through :meth:`MemoryStore.apply_with_capacity` -- the SAME store used
     by the live ``memory`` tool, just entered through a capacity-aware path
     instead of the auto-queue-on-overflow one, since replaying an
     already-queued record must never enqueue a duplicate of itself.
  3. Resolution:
       * success                -> ``pq.mark_done`` (terminal, not deleted)
       * stale replace/remove/  -> ``pq.mark_conflict`` (dead-letter now --
         batch (hash mismatch)     retrying a deterministic conflict can
                                    never succeed without a human relooking)
       * eviction couldn't free -> ``pq.mark_conflict`` (dead-letter now --
         enough room                same reasoning: unpinning/raising the
                                    limit requires operator action)
       * any other failure      -> ``pq.mark_failed`` (retries up to
         (malformed op, no        ``max_attempts``, then auto-dead-letters --
         match, disk error)       see ``memory_pending_queue.mark_failed``)

Idempotency:
  * ``add`` is a no-op success if the content is already present (covers a
    lease-expiry-then-reclaim double-apply, or a record that was already
    manually approved via ``/memory approve``).
  * Overflow ``replace``/``remove``/``batch`` use the same content
    idempotency (already-applied replace/remove is ``done``). Snapshot-hash
    mismatches dead-letter only on genuine external drift -- a sibling
    overflow write that landed first is not external drift.

Capacity handling (the reason this module exists rather than just calling
``tools.memory_tool.apply_memory_pending`` in a loop): when the projected
result would exceed the store's char limit, :meth:`MemoryStore.
apply_with_capacity` deterministically evicts the oldest evictable entries
(unpinned, not part of this same write) instead of re-queuing -- re-queuing
the same operation would loop forever against a store that never has room
freed for it. Evicted entries are archived via ``pq.record_eviction``
(SQLite, never deleted) for later recovery/audit; see that module and
``PIN_MARKER`` in ``tools.memory_tool`` for the eviction/priority policy.

This module intentionally does NO semantic consolidation (no LLM calls, no
merging similar facts, no rewriting text) -- that is a later phase. It only
replays exactly what was queued, or evicts by deterministic rule to make
room for it.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from tools import memory_pending_queue as pq
from tools.memory_tool import ENTRY_DELIMITER, MemoryStore, load_on_disk_store

logger = logging.getLogger(__name__)

# A projection is considered "behind" once the oldest active record has been
# waiting this long -- used only by get_status()'s `behind` flag, not to
# change consumer behavior. Conservative default: an hour of a healthy
# consumer running periodically should always clear the backlog well before
# this.
DEFAULT_STALE_AFTER_SECONDS = 3600.0


def _overflow_siblings(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Other overflow writes on the same target that can change the snapshot.

    Includes still-active siblings and siblings marked done after this
    record was queued (already applied in this drain pass).
    """
    rec_id = record.get("id")
    target = record.get("target")
    queued_at = record.get("created_at") or 0.0
    siblings: List[Dict[str, Any]] = []
    for other in pq.list_all(kind=pq.KIND_OVERFLOW):
        if other.get("id") == rec_id or other.get("target") != target:
            continue
        status = other.get("status")
        if status in pq.ACTIVE_STATUSES:
            siblings.append(other)
        elif status == pq.STATUS_DONE and (other.get("updated_at") or 0.0) >= queued_at:
            siblings.append(other)
    return siblings


def _external_overflow_drift(record: Dict[str, Any], store: MemoryStore, target: str) -> bool:
    """True only when a stamped hash mismatches and no sibling explains it."""
    if not record.get("expected_previous_hash"):
        return False
    current_hash = pq.content_snapshot_hash(
        ENTRY_DELIMITER.join(store._entries_for(target))
    )
    if not pq.is_stale(record, current_hash):
        return False
    return not _overflow_siblings(record)


def _apply_claimed(record: Dict[str, Any], store: MemoryStore) -> Dict[str, Any]:
    """Apply one claimed journal record to *store*. Never raises for
    ordinary application failures -- returns ``{"outcome": ..., "error": ...}``
    (outcome is one of ``done``/``retry``/``dead``). May raise on a genuine
    unexpected error (e.g. disk I/O); the caller treats that as ``retry``.
    """
    action = record["action"]
    target = record["target"]
    payload = record.get("payload") or {}

    if action in {"replace", "remove", "batch"}:
        kind = record.get("kind")
        if kind == pq.KIND_OVERFLOW:
            if _external_overflow_drift(record, store, target):
                return {
                    "outcome": "dead",
                    "error": (
                        f"Stale {action}: memory target '{target}' changed since "
                        "this write was queued (external drift; no sibling "
                        "overflow write explains the hash mismatch). "
                        "Not replayed. Review the current entries manually."
                    ),
                }
        else:
            expected_hash = record.get("expected_previous_hash")
            if expected_hash:
                current_hash = pq.content_snapshot_hash(
                    ENTRY_DELIMITER.join(store._entries_for(target))
                )
                if pq.is_stale(record, current_hash):
                    return {
                        "outcome": "dead",
                        "error": (
                            f"Stale {action}: memory target '{target}' changed since "
                            "this write was queued (expected_previous_hash mismatch). "
                            "Not replayed -- blindly applying could clobber a newer "
                            "edit. Review the current entries manually."
                        ),
                    }

    if action == "add":
        content = (payload.get("content") or "").strip()
        if not content:
            return {"outcome": "dead", "error": "add record has empty content."}
        if content in store._entries_for(target):
            # Already present -- a prior lease holder applied it before its
            # lease expired, or it was approved manually in the meantime.
            return {"outcome": "done", "result": {"idempotent": True}, "evicted": []}
        operations = [{"action": "add", "content": content}]

    elif action == "replace":
        content = (payload.get("content") or "").strip()
        old_text = (payload.get("old_text") or "").strip()
        entries = store._entries_for(target)
        if content and content in entries:
            others = [entry for entry in entries if entry != content]
            if not any(old_text in entry for entry in others):
                return {"outcome": "done", "result": {"idempotent": True}, "evicted": []}
        operations = [{
            "action": "replace",
            "old_text": old_text,
            "content": content,
        }]

    elif action == "remove":
        old_text = (payload.get("old_text") or "").strip()
        if old_text and not any(old_text in entry for entry in store._entries_for(target)):
            return {"outcome": "done", "result": {"idempotent": True}, "evicted": []}
        operations = [{"action": "remove", "old_text": old_text}]

    elif action == "batch":
        operations = payload.get("operations") or []
        if not operations:
            return {"outcome": "dead", "error": "batch record has no operations."}

    else:
        return {"outcome": "dead", "error": f"Unknown journal action '{action}'."}

    result = store.apply_with_capacity(target, operations)
    if result.get("success"):
        return {"outcome": "done", "result": result, "evicted": result.get("evicted") or []}
    if result.get("unresolvable"):
        return {"outcome": "dead", "error": result.get("error", "unresolvable")}
    if result.get("drift_backup"):
        return {"outcome": "dead", "error": result.get("error", "external drift")}
    return {"outcome": "retry", "error": result.get("error", "apply failed")}


def claim_and_apply(
    owner: Optional[str] = None,
    *,
    kind: Optional[str] = None,
    lease_seconds: float = pq.DEFAULT_LEASE_SECONDS,
    max_attempts: int = pq.DEFAULT_MAX_ATTEMPTS,
    retry_delay_seconds: float = pq.DEFAULT_RETRY_DELAY_SECONDS,
    store: Optional[MemoryStore] = None,
) -> Optional[Dict[str, Any]]:
    """Claim and resolve exactly one journal record. Returns ``None`` when
    nothing is available to claim.

    ``owner`` identifies this consumer instance for the lease (defaults to a
    fresh random id -- pass a stable value across calls in the same process
    so a crash's abandoned lease is attributable). ``store``, when given, is
    reused across calls (e.g. by :func:`run_once`'s loop) instead of
    reloading from disk each time; each mutation still re-reads under lock
    internally (see ``MemoryStore._reload_target``), so reuse is safe and
    just saves a redundant top-level load.
    """
    owner = owner or f"projection-{uuid.uuid4().hex[:8]}"
    record = pq.claim_next(owner, kind=kind, lease_seconds=lease_seconds)
    if record is None:
        return None

    active_store = store if store is not None else load_on_disk_store()

    try:
        outcome = _apply_claimed(record, active_store)
    except Exception as e:  # pragma: no cover - defensive, exercised via mocks in tests
        logger.exception(
            "memory_projection: unexpected error applying record %s", record["id"]
        )
        outcome = {"outcome": "retry", "error": f"unexpected error: {e}"}

    status = outcome["outcome"]
    evicted: List[str] = outcome.get("evicted") or []

    if status == "done":
        for text in evicted:
            pq.record_eviction(
                record["target"], text, evicted_by_id=record["id"], reason="capacity"
            )
        resolved = pq.mark_done(record["id"], owner)
    elif status == "dead":
        resolved = pq.mark_conflict(record["id"], owner, outcome.get("error", "unresolvable"))
    else:
        resolved_status = pq.mark_failed(
            record["id"], owner, outcome.get("error", "apply failed"),
            max_attempts=max_attempts, retry_delay_seconds=retry_delay_seconds,
        )
        resolved = resolved_status is not None
        # mark_failed decides retry-vs-dead-letter based on attempts vs
        # max_attempts; reflect its actual decision rather than the
        # pre-resolution "retry" label from _apply_claimed.
        if resolved_status == pq.STATUS_DEAD:
            status = "dead"

    if not resolved:
        # Lease was reclaimed by another consumer before we could resolve
        # (e.g. this call ran past `lease_seconds`) -- our write attempt may
        # or may not have landed, but we no longer own the row, so we must
        # not touch it further. The next claimant will see current disk
        # state and either find the entry already present (idempotent) or
        # retry cleanly.
        status = "lease_lost"

    return {
        "id": record["id"],
        "kind": record["kind"],
        "action": record["action"],
        "target": record["target"],
        "outcome": status,
        "error": outcome.get("error"),
        "evicted": evicted,
    }


def run_once(
    owner: Optional[str] = None,
    *,
    max_records: int = 100,
    kind: Optional[str] = None,
    store: Optional[MemoryStore] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Drain up to ``max_records`` journal records in one pass.

    Reuses a single :class:`MemoryStore` load across the whole pass (each
    claimed record still re-reads the on-disk file under lock before
    mutating, so this is a performance choice, not a correctness one).
    Returns a summary dict: ``{"owner", "processed", "counts", "results"}``
    where ``counts`` tallies outcomes (``done``/``retry``/``dead``/
    ``lease_lost``).
    """
    owner = owner or f"projection-{uuid.uuid4().hex[:8]}"
    active_store = store if store is not None else load_on_disk_store()
    results: List[Dict[str, Any]] = []
    for _ in range(max_records):
        r = claim_and_apply(owner, kind=kind, store=active_store, **kwargs)
        if r is None:
            break
        results.append(r)

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1

    return {"owner": owner, "processed": len(results), "counts": counts, "results": results}


def get_status(
    *, kind: Optional[str] = None, stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
) -> Dict[str, Any]:
    """Bounded health summary of the journal/projection -- safe to poll and
    to surface in a status line or dashboard widget.

    Deliberately returns fixed-size scalars, never the pending records'
    content -- callers that need the actual queued writes should use
    ``memory_pending_queue.list_active`` (or ``/memory pending``) directly,
    not inject this into a prompt.
    """
    active = pq.list_active(kind=kind)
    now = time.time()
    oldest_age = max((now - r["created_at"] for r in active), default=0.0)
    failed = [r for r in active if r["status"] == pq.STATUS_FAILED]
    processing = [r for r in active if r["status"] == pq.STATUS_PROCESSING]
    dead_letter_count = len(pq.list_all(kind=kind, status=pq.STATUS_DEAD))

    error_candidates = [r for r in active if r.get("error_detail")]
    last_error_record = max(
        error_candidates, key=lambda r: r["updated_at"], default=None
    )

    behind = bool(active) and (
        oldest_age > stale_after_seconds
        or len(failed) > 0
        or bool(error_candidates)
    )

    evicted_count = pq.count_evictions()

    fallback = False
    try:
        from tools.memory_tool import get_memory_dir
        fallback_path = get_memory_dir() / "pending_fallback.jsonl"
        fallback = fallback_path.exists() and fallback_path.stat().st_size > 0
    except OSError:
        fallback = False

    return {
        "active_count": len(active),
        "pending_count": sum(1 for r in active if r["status"] == pq.STATUS_PENDING),
        "processing_count": len(processing),
        "failed_count": len(failed),
        "dead_letter_count": dead_letter_count,
        "evicted_count": evicted_count,
        "oldest_age_seconds": oldest_age,
        "last_error": last_error_record.get("error_detail") if last_error_record else None,
        "last_error_record_id": last_error_record["id"] if last_error_record else None,
        "behind": behind,
        "stale_after_seconds": stale_after_seconds,
        "fallback": fallback,
    }


def list_dead_letters(*, kind: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Most-recently dead-lettered records first, bounded by ``limit``."""
    records = pq.list_all(kind=kind, status=pq.STATUS_DEAD)
    tail = records[-limit:] if limit else records
    return list(reversed(tail))
