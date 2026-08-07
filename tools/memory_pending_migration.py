#!/usr/bin/env python3
"""One-time, idempotent import of pre-SQLite-journal legacy pending JSON files.

Before ``tools/memory_pending_queue.py`` existed, two independent mechanisms
wrote best-effort JSON files that this module now folds into that shared
SQLite journal:

  * ``~/.hermes/memories/pending/*.json`` -- memory_tool overflow writes
    (schema: ``{schema, queued_at, target, operations, usage}``).
  * ``~/.hermes/pending/memory/*.json``   -- write_approval staged memory
    writes (schema: ``{id, subsystem, action, summary, origin, created_at,
    payload}``).

Neither location ever had a consumer that read and deleted files -- they were
purely a durable-but-unconsumed record, the same status quo as an unresolved
row in the new queue. So every file found here is imported as a ``pending``
record; nothing to migrate is ever a done/dead record.

Safety properties, mirroring the ones ``memory_pending_queue`` itself
provides:

  * **Idempotent / crash-safe.** A file is archived (moved into a ``migrated/``
    sibling directory) only *after* the DB insert commits. If the process
    dies between commit and archive, the next run's :func:`import_legacy`
    call sees the id already present and no-ops, then completes the archive.
    If it dies before the commit, nothing changed and the file is untouched.
  * **No data loss on failure.** A file that fails to parse or is missing
    required fields is left exactly where it is -- never deleted, never
    silently dropped -- and reported in the returned summary so the failure
    is visible. Fix the file (or the underlying cause, e.g. a full queue) and
    the next call retries it.
  * **Concurrency-safe.** Two processes (or threads) racing the same file
    both attempt the same deterministic id; the loser's insert is a no-op
    (see :func:`tools.memory_pending_queue.import_legacy`) and its archive
    attempt on an already-moved file is caught and ignored.
  * **Originals are never deleted**, only moved once safely durable elsewhere.

Known, honest limitation: the legacy overflow schema never recorded a
"previous content" snapshot, so migrated overflow ``replace``/``remove``
records carry no ``expected_previous_hash`` and can never be flagged as a
stale conflict (see ``pq.is_stale``) -- that staleness detection only exists
for operations queued after this queue itself existed.

This module provides import into the durable journal only. It intentionally
does not apply, approve, or otherwise act on migrated records -- that still
goes through the existing ``/memory pending`` review path (or the semantic
curator/projection consumer, which is a later, not-yet-built phase; see the
module docstring of ``memory_pending_queue`` for what "durable" does and does
not mean today).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

MIGRATED_DIRNAME = "migrated"

# Reentrancy guard: memory_pending_queue._connection() auto-triggers a
# migration pass the first time it opens a connection on a given resolved db
# path -- but each imported record's own pq.import_legacy() call opens a
# *nested* connection on that same path. Without this guard, that nested
# open would see the path not-yet-marked-migrated (the outer explicit call
# only marks it inside _connection(), same as any other caller) and kick off
# a second, concurrent migration pass on the same thread, racing the outer
# pass's file archiving. Thread-local (not a plain flag) so unrelated
# threads still each get to run migration once.
_REENTRANCY_GUARD = threading.local()


def legacy_overflow_dir() -> Path:
    """``~/.hermes/memories/pending`` -- pre-queue memory_tool overflow files."""
    return get_hermes_home() / "memories" / "pending"


def legacy_approval_dir() -> Path:
    """``~/.hermes/pending/memory`` -- pre-queue write_approval staged files."""
    return get_hermes_home() / "pending" / "memory"


def _archive(path: Path) -> None:
    """Move a successfully-imported legacy file into a ``migrated/`` sibling.

    Never deletes. ``Path.replace`` is an atomic rename within the same
    filesystem (both source and destination are under the same HERMES_HOME
    tree), so a second caller racing the same file simply finds it already
    gone -- that is success, not an error.
    """
    dest_dir = path.parent / MIGRATED_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest_dir.chmod(0o700)
    except OSError:
        pass
    dest = dest_dir / path.name
    try:
        path.replace(dest)
    except FileNotFoundError:
        pass  # another process already archived it


def _coerce_created_at(raw: Any, path: Path) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _migrate_overflow_file(path: Path, pq, *, cap: int) -> Dict[str, Any]:
    # Another concurrent migrator may have imported and archived this path
    # after the caller enumerated it. That is successful idempotent work.
    if not path.exists():
        return {"file": str(path), "ok": True, "inserted": False, "already_archived": True}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"file": str(path), "ok": False, "error": f"malformed JSON: {e}"}

    if not isinstance(raw, dict):
        return {"file": str(path), "ok": False, "error": "not a JSON object"}

    operations = raw.get("operations")
    target = raw.get("target")
    if not isinstance(operations, list) or not operations or not isinstance(target, str) or not target:
        return {"file": str(path), "ok": False, "error": "missing/invalid target or operations"}

    if len(operations) > 1:
        action = "batch"
        payload: Dict[str, Any] = {"action": "batch", "target": target, "operations": operations}
    else:
        op = operations[0]
        if not isinstance(op, dict) or not op.get("action"):
            return {"file": str(path), "ok": False, "error": "operation missing action"}
        action = op["action"]
        payload = {
            "action": action,
            "target": target,
            "content": op.get("content"),
            "old_text": op.get("old_text"),
        }

    created_at = _coerce_created_at(raw.get("queued_at"), path)
    id_hint = f"legacy-overflow-{path.stem}"

    try:
        record, inserted = pq.import_legacy(
            id_hint, pq.KIND_OVERFLOW, action, target, payload,
            summary=f"overflow {action} on {target} (migrated from legacy queue)",
            origin="foreground",
            expected_previous_hash=None,
            created_at=created_at,
            cap=cap,
        )
    except pq.QueueFullError as e:
        return {"file": str(path), "ok": False, "error": str(e), "retryable": True}
    except Exception as e:
        logger.error("Failed to import legacy overflow file %s: %s", path, e, exc_info=True)
        return {"file": str(path), "ok": False, "error": str(e)}

    _archive(path)
    return {"file": str(path), "ok": True, "id": record["id"], "inserted": inserted}


def _migrate_approval_file(path: Path, pq, *, cap: int) -> Dict[str, Any]:
    # Another concurrent migrator may have imported and archived this path
    # after the caller enumerated it. That is successful idempotent work.
    if not path.exists():
        return {"file": str(path), "ok": True, "inserted": False, "already_archived": True}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"file": str(path), "ok": False, "error": f"malformed JSON: {e}"}

    if not isinstance(raw, dict):
        return {"file": str(path), "ok": False, "error": "not a JSON object"}

    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return {"file": str(path), "ok": False, "error": "missing payload"}

    action = payload.get("action") or raw.get("action")
    target = payload.get("target") or "memory"
    if not action:
        return {"file": str(path), "ok": False, "error": "missing action"}

    created_at = _coerce_created_at(raw.get("created_at"), path)
    legacy_id = raw.get("id") or path.stem
    id_hint = f"legacy-approval-{legacy_id}"

    try:
        record, inserted = pq.import_legacy(
            id_hint, pq.KIND_APPROVAL, action, target, payload,
            summary=raw.get("summary", "") or "",
            origin=raw.get("origin", "foreground") or "foreground",
            expected_previous_hash=payload.get("expected_previous_hash"),
            created_at=created_at,
            cap=cap,
        )
    except pq.QueueFullError as e:
        return {"file": str(path), "ok": False, "error": str(e), "retryable": True}
    except Exception as e:
        logger.error("Failed to import legacy approval file %s: %s", path, e, exc_info=True)
        return {"file": str(path), "ok": False, "error": str(e)}

    _archive(path)
    return {"file": str(path), "ok": True, "id": record["id"], "inserted": inserted}


def migrate_legacy_pending(*, cap: Optional[int] = None) -> Dict[str, Any]:
    """Import any pre-existing legacy JSON pending files into the SQLite journal.

    Safe to call repeatedly (idempotent) and safe under concurrent callers.
    Returns ``{"migrated": [...], "failed": [...]}``; each entry is a small
    dict identifying the source file and outcome. A non-empty ``failed`` list
    is logged as a warning -- those files are untouched and will be retried
    on the next call (e.g. the next process start, or the next explicit
    invocation from a CLI inspection command).
    """
    if getattr(_REENTRANCY_GUARD, "active", False):
        # A queue import opens a nested queue connection. The nested
        # connection's automatic migration hook must not enumerate/archive
        # the same legacy files while the outer pass is working.
        return {"migrated": [], "failed": []}

    _REENTRANCY_GUARD.active = True
    try:
        from tools import memory_pending_queue as pq

        effective_cap = pq.DEFAULT_QUEUE_CAP if cap is None else cap

        jobs: List[tuple] = []
        for d, migrate_fn in (
            (legacy_overflow_dir(), _migrate_overflow_file),
            (legacy_approval_dir(), _migrate_approval_file),
        ):
            if not d.exists():
                continue
            for path in sorted(d.glob("*.json")):
                jobs.append((path, migrate_fn))

        results: List[Dict[str, Any]] = [
            migrate_fn(path, pq, cap=effective_cap) for path, migrate_fn in jobs
        ]

        migrated = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
        if failed:
            logger.warning(
                "Legacy pending migration: %d file(s) failed and will be retried later: %s",
                len(failed), [f["file"] for f in failed],
            )
        return {"migrated": migrated, "failed": failed}
    finally:
        _REENTRANCY_GUARD.active = False
