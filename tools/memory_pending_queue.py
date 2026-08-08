#!/usr/bin/env python3
"""Durable, crash-safe pending-operation queue for memory writes.

Single shared backing store for BOTH mechanisms that previously wrote
separate best-effort JSON files:

  * **overflow** writes that don't fit in the bounded MEMORY.md/USER.md store
    (``tools/memory_tool.py`` -- used to write mode-0600 JSON files under
    ``memories/pending/``)
  * **approval** writes staged for user review under ``memory.write_approval``
    (``tools/write_approval.py`` -- used to write JSON files under
    ``pending/memory/``)

Backed by SQLite in WAL journal mode with ``synchronous=FULL``: every
enqueue/claim/resolve is a committed transaction, so a crash between "we
told the model the write is durable" and "the row hit disk" cannot happen --
either the transaction committed (fsynced) or it didn't run.

Deliberately a SEPARATE database file (``memory_pending.db``) from
``state.db``, the high-throughput per-turn session store -- this queue is
low-volume (one row per overflow/staged write, not one per turn) and its
durability profile (fsync every write, no batching) would be the wrong
trade-off applied to session state.

Reuses existing hardened SQLite infrastructure rather than re-inventing it:

  * :func:`hermes_state.apply_wal_with_fallback` -- the same WAL-with-NFS/
    WAL-reset-bug fallback that ``kanban_db.py`` and (indirectly) session
    state use.
  * :func:`hermes_state.preflight_db_writability` -- refuse-or-repair a
    stray read-only db/-wal/-shm before the first connection.
  * :func:`hermes_cli.sqlite_util.write_txn` -- the shared ``BEGIN
    IMMEDIATE`` / commit-or-rollback primitive used by the projects and
    kanban stores.

Record lifecycle (``status``): ``pending`` -> ``processing`` (claimed, under
lease) -> ``done`` | ``failed`` (retryable, back to ``pending`` until
``attempts`` exhausts) -> ``dead`` (terminal: exhausted retries, a discarded
staged write, or a stale replace/remove conflict). A record is NEVER deleted
as part of normal processing -- completion is recorded by status transition,
not by unlinking a file / row. Delivery is at-least-once: a lease can expire
after a consumer applies a write but before it acknowledges, so consumers must
make application idempotent or use expected-version checks. Rows are
dead-lettered by status, not by disappearing.

Legacy JSON migration: any pre-existing JSON files under
``memories/pending/*.json`` (memory_tool overflow) or ``pending/memory/*.json``
(write_approval staging) from before this module existed are imported
automatically, once per process, the first time this module opens a
connection (see the migration trigger in :func:`_connection` and
:mod:`tools.memory_pending_migration` for the full idempotency/crash-safety
contract). Imported records are visible to ``/memory pending`` immediately.
Originals are archived into a ``migrated/`` sibling directory after a
successful import -- never deleted -- and a file that fails to import is left
in place and retried on the next call.

This module provides durable journal + review storage only. There is
currently no automated consumer that reviews and applies queued records on
its own -- resolution happens via the existing ``/memory pending`` /
``/memory approve`` / ``/memory reject`` commands (or direct claim/lease API
for a future curator process). Do not describe this queue as having an
automatic "curator" that reviews and merges entries; none exists yet.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from hermes_cli.sqlite_util import write_txn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KIND_OVERFLOW = "overflow"
KIND_APPROVAL = "approval"
_KINDS = (KIND_OVERFLOW, KIND_APPROVAL)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"
STATUS_DONE = "done"

# Statuses that occupy queue capacity / are still "in flight" for a curator.
_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED)
# Public alias for adapters that need to distinguish active from terminal rows.
ACTIVE_STATUSES = _ACTIVE_STATUSES

# Conservative default cap on concurrently unresolved (active) records. This
# bounds the queue's disk footprint and gives a clear terminal failure instead
# of unbounded growth when a curator/approver is absent for a long time.
DEFAULT_QUEUE_CAP = 500

DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_DELAY_SECONDS = 60.0


class QueueFullError(RuntimeError):
    """Raised by :func:`enqueue` when the active-record cap is reached.

    Callers must treat this as a clear terminal failure -- do not catch it and
    silently drop the write; surface it to the caller/user.
    """

    def __init__(self, cap: int):
        super().__init__(
            f"Pending memory queue is full ({cap} unresolved records). "
            "Review and resolve pending writes (approve/reject or let the "
            "curator process them) before more can be queued."
        )
        self.cap = cap


# ---------------------------------------------------------------------------
# Schema + connection
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pending_ops (
    id                      TEXT PRIMARY KEY,
    kind                    TEXT NOT NULL,
    action                  TEXT NOT NULL,
    target                  TEXT NOT NULL,
    payload                 TEXT NOT NULL,
    summary                 TEXT NOT NULL DEFAULT '',
    origin                  TEXT NOT NULL DEFAULT 'foreground',
    content_hash            TEXT NOT NULL,
    expected_previous_hash  TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending',
    attempts                INTEGER NOT NULL DEFAULT 0,
    next_attempt_at         REAL,
    lease_owner             TEXT,
    lease_expires_at        REAL,
    error_detail            TEXT,
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_ops_status_created
    ON pending_ops(status, created_at);

CREATE INDEX IF NOT EXISTS idx_pending_ops_content_hash
    ON pending_ops(content_hash);

-- Audit trail for entries the deterministic projection consumer evicted from
-- a full MEMORY.md/USER.md store to make room for an applied journal record
-- (see tools/memory_projection.py). Rows are append-only: eviction is never
-- silent data loss -- the original entry text is preserved here for the user
-- to recover (a future ``/memory evicted`` review surface, or manual re-add)
-- even though it is gone from the bounded live store.
CREATE TABLE IF NOT EXISTS evicted_entries (
    id                TEXT PRIMARY KEY,
    target            TEXT NOT NULL,
    entry_text        TEXT NOT NULL,
    evicted_by_id     TEXT,
    reason            TEXT NOT NULL DEFAULT '',
    evicted_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evicted_entries_target_time
    ON evicted_entries(target, evicted_at);
"""

# Guards schema init + WAL activation, mirroring kanban_db.py's _INIT_LOCK:
# keeps concurrent same-process callers from racing through CREATE TABLE /
# PRAGMA with stale snapshots.
_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set = set()

# Guards the legacy-JSON migration trigger (see tools/memory_pending_migration).
# Separate from _INIT_LOCK because migration itself opens nested connections
# (pq.import_legacy -> _connection()) on the same thread; a single non-
# reentrant lock held across the migration call would deadlock. Instead this
# lock is held only long enough to claim the "run migration" flag, which is
# set BEFORE migration runs -- so the nested _connection() calls the
# migration makes see the path already claimed and skip straight through.
_MIGRATION_LOCK = threading.Lock()
_MIGRATED_PATHS: set = set()


def queue_db_path() -> Path:
    """Profile-scoped path of the shared pending-operation database."""
    return get_hermes_home() / "memory_pending.db"


def _restrict_permissions(path: Path, *, is_dir: bool) -> None:
    """Best-effort chmod to owner-only (0700 dir / 0600 file).

    Pending records can contain arbitrary user-authored memory content;
    treat the store like the JSON pending files it replaces (mode 0600).
    """
    try:
        mode = 0o700 if is_dir else 0o600
        path.chmod(mode)
    except OSError:
        pass


@contextlib.contextmanager
def _connection():
    """Open a connection to the pending-op DB, initializing it if needed.

    Every public function in this module opens-and-closes its own short-lived
    connection (no long-lived module-level handle) so a crash never leaves a
    dangling lock and every operation's durability is independent of process
    lifetime.
    """
    path = queue_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    from hermes_state import apply_wal_with_fallback, preflight_db_writability

    preflight_db_writability(path, db_label="memory_pending.db")

    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        resolved = str(path.resolve())
        with _INIT_LOCK:
            apply_wal_with_fallback(conn, db_label="memory_pending.db")
            # fsync before each checkpoint -- this queue's whole purpose is
            # "durable means durable", so best-effort NORMAL is not enough.
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            if resolved not in _INITIALIZED_PATHS:
                conn.executescript(SCHEMA_SQL)
                _INITIALIZED_PATHS.add(resolved)
        _restrict_permissions(path, is_dir=False)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                _restrict_permissions(sidecar, is_dir=False)

        run_migration = False
        if resolved not in _MIGRATED_PATHS:
            with _MIGRATION_LOCK:
                if resolved not in _MIGRATED_PATHS:
                    _MIGRATED_PATHS.add(resolved)
                    run_migration = True
        if run_migration:
            try:
                from tools import memory_pending_migration as _mig
                _mig.migrate_legacy_pending()
            except Exception:
                logger.exception("Legacy pending-queue migration failed")

        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def content_hash(kind: str, action: str, target: str, payload: Dict[str, Any]) -> str:
    """Deterministic idempotency key for one logical operation.

    Two enqueue calls with identical (kind, action, target, payload) hash
    identically -- used to dedupe a retried/duplicate enqueue instead of
    creating a second queue entry for the same write.
    """
    blob = json.dumps(
        {"kind": kind, "action": action, "target": target, "payload": payload},
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def content_snapshot_hash(text: str) -> str:
    """Hash of a target store's current serialized content.

    Callers use this to stamp ``expected_previous_hash`` at enqueue time (from
    the entries the operation was validated against) and to recompute the
    "current" hash at apply time -- a mismatch means the store changed after
    the operation was queued (see :func:`is_stale`).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_stale(record: Dict[str, Any], current_hash: Optional[str]) -> bool:
    """True when a replace/remove record's snapshot no longer matches reality.

    A record with no ``expected_previous_hash`` (pure appends, which never
    clobber existing content) is never stale.
    """
    expected = record.get("expected_previous_hash")
    if not expected:
        return False
    return expected != current_hash


# ---------------------------------------------------------------------------
# Row <-> record
# ---------------------------------------------------------------------------

def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
    rec = dict(row)
    try:
        rec["payload"] = json.loads(rec["payload"]) if rec.get("payload") else {}
    except (TypeError, ValueError):
        rec["payload"] = {}
    return rec


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue(
    kind: str,
    action: str,
    target: str,
    payload: Dict[str, Any],
    *,
    summary: str = "",
    origin: str = "foreground",
    expected_previous_hash: Optional[str] = None,
    cap: int = DEFAULT_QUEUE_CAP,
) -> Dict[str, Any]:
    """Durably enqueue one operation. Idempotent on (kind, action, target, payload).

    Returns the record dict (existing one if this was a duplicate of an
    already-active record, otherwise the newly inserted one).

    Raises :class:`QueueFullError` -- without inserting a row -- when the
    number of active (pending/processing/failed) records has already reached
    ``cap``. This is a clear terminal failure, never a silent drop.
    """
    if kind not in _KINDS:
        raise ValueError(f"invalid kind {kind!r}; use one of {_KINDS}")

    chash = content_hash(kind, action, target, payload)
    now = time.time()

    with _connection() as conn:
        with write_txn(conn):
            existing = conn.execute(
                "SELECT * FROM pending_ops WHERE content_hash=? AND status IN (?,?,?) "
                "ORDER BY created_at LIMIT 1",
                (chash, STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED),
            ).fetchone()
            if existing is not None:
                return _row_to_record(existing)

            active_count = conn.execute(
                "SELECT COUNT(*) FROM pending_ops WHERE status IN (?,?,?)",
                (STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED),
            ).fetchone()[0]
            if active_count >= cap:
                raise QueueFullError(cap)

            rec_id = uuid.uuid4().hex[:12]
            conn.execute(
                """
                INSERT INTO pending_ops (
                    id, kind, action, target, payload, summary, origin,
                    content_hash, expected_previous_hash, status, attempts,
                    next_attempt_at, lease_owner, lease_expires_at,
                    error_detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    rec_id, kind, action, target,
                    json.dumps(payload, ensure_ascii=False),
                    summary or "", origin or "foreground",
                    chash, expected_previous_hash, STATUS_PENDING,
                    now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM pending_ops WHERE id=?", (rec_id,)
            ).fetchone()
            return _row_to_record(row)


def import_legacy(
    id_hint: str,
    kind: str,
    action: str,
    target: str,
    payload: Dict[str, Any],
    *,
    summary: str = "",
    origin: str = "foreground",
    expected_previous_hash: Optional[str] = None,
    created_at: Optional[float] = None,
    cap: int = DEFAULT_QUEUE_CAP,
) -> "tuple[Dict[str, Any], bool]":
    """Idempotently import one pre-existing legacy JSON pending record.

    Used only by :mod:`tools.memory_pending_migration` to fold the old
    ``memories/pending/*.json`` and ``pending/memory/*.json`` files into this
    database. Distinct from :func:`enqueue` in one deliberate way: dedup is
    keyed primarily on ``id_hint`` -- a deterministic id the caller derives
    from the legacy file's identity (e.g. its filename) -- rather than on
    active-only content hash. That makes re-running migration against a
    legacy file that hasn't been archived yet a true no-op even after the
    imported record reaches a terminal status, which matters because
    migration only archives (moves) a legacy file *after* a successful
    import; a crash in between must not risk a duplicate on retry.

    Falls back to the normal active content-hash dedup so a legacy record
    that some other path already re-enqueued live (same kind/action/target/
    payload, different id) is not duplicated either.

    Always inserted as ``pending`` -- both legacy stores only ever kept
    unresolved records on disk (nothing consumed or deleted them before this
    queue existed).

    Returns ``(record, inserted)`` where ``inserted`` is False when an
    existing row (by id or by content hash) was returned instead of a new
    one being created.

    Raises :class:`QueueFullError` -- without inserting a row -- under the
    same cap semantics as :func:`enqueue`, so a full queue leaves the legacy
    file unarchived (and therefore retried on the next migration pass)
    instead of silently losing the record.
    """
    if kind not in _KINDS:
        raise ValueError(f"invalid kind {kind!r}; use one of {_KINDS}")
    if not id_hint:
        raise ValueError("id_hint is required")

    chash = content_hash(kind, action, target, payload)
    now = time.time()
    created = now if created_at is None else float(created_at)

    with _connection() as conn:
        with write_txn(conn):
            existing_by_id = conn.execute(
                "SELECT * FROM pending_ops WHERE id=?", (id_hint,)
            ).fetchone()
            if existing_by_id is not None:
                return _row_to_record(existing_by_id), False

            existing_by_hash = conn.execute(
                "SELECT * FROM pending_ops WHERE content_hash=? AND status IN (?,?,?) "
                "ORDER BY created_at LIMIT 1",
                (chash, STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED),
            ).fetchone()
            if existing_by_hash is not None:
                return _row_to_record(existing_by_hash), False

            active_count = conn.execute(
                "SELECT COUNT(*) FROM pending_ops WHERE status IN (?,?,?)",
                (STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED),
            ).fetchone()[0]
            if active_count >= cap:
                raise QueueFullError(cap)

            conn.execute(
                """
                INSERT INTO pending_ops (
                    id, kind, action, target, payload, summary, origin,
                    content_hash, expected_previous_hash, status, attempts,
                    next_attempt_at, lease_owner, lease_expires_at,
                    error_detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    id_hint, kind, action, target,
                    json.dumps(payload, ensure_ascii=False),
                    summary or "", origin or "foreground",
                    chash, expected_previous_hash, STATUS_PENDING,
                    created, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM pending_ops WHERE id=?", (id_hint,)
            ).fetchone()
            return _row_to_record(row), True


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get(record_id: str) -> Optional[Dict[str, Any]]:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM pending_ops WHERE id=?", (record_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None


def list_all(*, kind: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """All records (including terminal done/dead), oldest first."""
    clauses, params = [], []
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM pending_ops {where} ORDER BY created_at", params
        ).fetchall()
        return [_row_to_record(r) for r in rows]


def list_active(*, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Records still awaiting resolution (pending/processing/failed), oldest first."""
    clauses = ["status IN (?,?,?)"]
    params: List[Any] = [STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED]
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    with _connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM pending_ops WHERE {' AND '.join(clauses)} ORDER BY created_at",
            params,
        ).fetchall()
        return [_row_to_record(r) for r in rows]


def count_active(*, kind: Optional[str] = None) -> int:
    clauses = ["status IN (?,?,?)"]
    params: List[Any] = [STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED]
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    with _connection() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM pending_ops WHERE {' AND '.join(clauses)}", params
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Discard (user rejection) -- status transition, never a delete
# ---------------------------------------------------------------------------

def discard(record_id: str, *, reason: str = "discarded by user") -> bool:
    """Mark an active record ``dead`` (rejected). Returns False if not active.

    Never deletes the row -- the record stays visible via :func:`get` /
    :func:`list_all` for audit, just excluded from :func:`list_active`.
    """
    now = time.time()
    with _connection() as conn:
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE pending_ops SET status=?, error_detail=?, updated_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL "
                "WHERE id=? AND status IN (?,?,?)",
                (STATUS_DEAD, reason, now, record_id,
                 STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Claim / lease (curator-facing)
# ---------------------------------------------------------------------------

def claim_next(
    owner: str,
    *,
    kind: Optional[str] = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest available record for ``owner``.

    "Available" means ``pending``, ``failed`` records whose retry delay has
    elapsed, or ``processing`` records whose lease has expired (a crashed
    curator's claim gets reclaimed). Safe under concurrent callers: the
    selection + update happen inside one ``BEGIN IMMEDIATE`` transaction, so
    two curators racing this call can never claim the same row.
    """
    now = time.time()
    clauses = [
        "(status=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)) "
        "OR (status=? AND (lease_expires_at IS NULL OR lease_expires_at<?))"
    ]
    params: List[Any] = [STATUS_PENDING, now, STATUS_PROCESSING, now]
    if kind is not None:
        clauses = [f"({c})" for c in clauses]
        where = f"({' OR '.join(clauses)}) AND kind=?"
        params.append(kind)
    else:
        where = " OR ".join(clauses)

    with _connection() as conn:
        with write_txn(conn):
            row = conn.execute(
                f"SELECT id FROM pending_ops WHERE {where} ORDER BY created_at LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            rec_id = row["id"]
            conn.execute(
                "UPDATE pending_ops SET status=?, lease_owner=?, lease_expires_at=?, "
                "attempts=attempts+1, updated_at=? WHERE id=?",
                (STATUS_PROCESSING, owner, now + lease_seconds, now, rec_id),
            )
            updated = conn.execute(
                "SELECT * FROM pending_ops WHERE id=?", (rec_id,)
            ).fetchone()
            return _row_to_record(updated)


def mark_done(record_id: str, owner: str) -> bool:
    """Resolve a claimed record as successfully applied.

    Requires the caller to hold the lease (``lease_owner == owner``) so a
    stale/expired claimant cannot mark a record another owner has since
    reclaimed and is processing. Status transition only -- the row is kept.
    """
    now = time.time()
    with _connection() as conn:
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE pending_ops SET status=?, lease_owner=NULL, "
                "lease_expires_at=NULL, error_detail=NULL, updated_at=? "
                "WHERE id=? AND lease_owner=?",
                (STATUS_DONE, now, record_id, owner),
            )
            return cur.rowcount > 0


def complete(record_id: str) -> bool:
    """Mark an active record done after an explicit approval/apply action.

    Approval handlers are not curator workers and therefore do not hold a
    lease. This transition is intentionally separate from ``mark_done``: it
    accepts an active pending/processing/failed record but still preserves the
    row for audit instead of deleting it.
    """
    now = time.time()
    with _connection() as conn:
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE pending_ops SET status=?, lease_owner=NULL, "
                "lease_expires_at=NULL, error_detail=NULL, updated_at=? "
                "WHERE id=? AND status IN (?,?,?)",
                (STATUS_DONE, now, record_id,
                 STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED),
            )
            return cur.rowcount > 0


def mark_failed(
    record_id: str,
    owner: str,
    error_detail: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
) -> Optional[str]:
    """Resolve a claimed record as failed; retry (pending) or dead-letter.

    Returns the resulting status (``"pending"`` or ``"dead"``), or ``None``
    if the caller did not hold the lease (already reclaimed / resolved
    elsewhere).
    """
    now = time.time()
    with _connection() as conn:
        with write_txn(conn):
            row = conn.execute(
                "SELECT attempts FROM pending_ops WHERE id=? AND lease_owner=?",
                (record_id, owner),
            ).fetchone()
            if row is None:
                return None
            attempts = row["attempts"]
            if attempts >= max_attempts:
                next_status = STATUS_DEAD
                next_attempt_at = None
            else:
                next_status = STATUS_PENDING
                next_attempt_at = now + retry_delay_seconds
            conn.execute(
                "UPDATE pending_ops SET status=?, next_attempt_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL, error_detail=?, "
                "updated_at=? WHERE id=? AND lease_owner=?",
                (next_status, next_attempt_at, error_detail, now, record_id, owner),
            )
            return next_status


def mark_conflict(record_id: str, owner: str, error_detail: str) -> bool:
    """Dead-letter a claimed record as a stale replace/remove conflict.

    Conflicts are not auto-retried (the world changed since it was queued --
    blindly retrying could clobber a newer edit); a human/curator must look
    at it. Requires the caller to hold the lease.
    """
    now = time.time()
    with _connection() as conn:
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE pending_ops SET status=?, lease_owner=NULL, "
                "lease_expires_at=NULL, error_detail=?, updated_at=? "
                "WHERE id=? AND lease_owner=?",
                (STATUS_DEAD, error_detail, now, record_id, owner),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Eviction audit trail (projection consumer -- see tools/memory_projection.py)
# ---------------------------------------------------------------------------

def record_eviction(
    target: str,
    entry_text: str,
    *,
    evicted_by_id: Optional[str] = None,
    reason: str = "capacity",
) -> Dict[str, Any]:
    """Durably archive one entry evicted from a bounded memory store.

    Never overwrites or deletes -- each call inserts a new row, so the same
    entry text evicted twice (e.g. re-added, then evicted again later) is
    recorded twice. ``evicted_by_id`` is the journal record whose application
    triggered the eviction, for traceability; it is NOT a foreign key
    (the pending_ops row may itself be pruned/archived independently).
    """
    now = time.time()
    rec_id = uuid.uuid4().hex[:12]
    with _connection() as conn:
        with write_txn(conn):
            conn.execute(
                "INSERT INTO evicted_entries "
                "(id, target, entry_text, evicted_by_id, reason, evicted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rec_id, target, entry_text, evicted_by_id, reason, now),
            )
    return {
        "id": rec_id,
        "target": target,
        "entry_text": entry_text,
        "evicted_by_id": evicted_by_id,
        "reason": reason,
        "evicted_at": now,
    }


def list_evictions(*, target: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Most recent evicted entries first, optionally filtered by target.

    Bounded by ``limit`` -- this is an audit/recovery surface, not something
    meant to be dumped into a prompt in full.
    """
    clauses, params = [], []
    if target is not None:
        clauses.append("target=?")
        params.append(target)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with _connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM evicted_entries {where} ORDER BY evicted_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def count_evictions(*, target: Optional[str] = None) -> int:
    clauses, params = [], []
    if target is not None:
        clauses.append("target=?")
        params.append(target)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connection() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM evicted_entries {where}", params
        ).fetchone()[0]
