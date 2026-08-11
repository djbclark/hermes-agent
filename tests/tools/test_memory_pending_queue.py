"""Tests for tools/memory_pending_queue.py -- the shared durable pending-op
queue used by both memory_tool's overflow path and write_approval's staging.

Uses a real temp HERMES_HOME (per AGENTS.md: exercise the real SQLite path,
not a mock) for every test via the ``hermes_home`` fixture.
"""

import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_pq_test_")
    home = os.path.join(d, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_init_cache():
    """_INITIALIZED_PATHS is process-global; each test gets its own tmp
    HERMES_HOME/db path so stale cache entries from other tests are harmless,
    but clear it anyway so schema-init assertions are meaningful per test."""
    import tools.memory_pending_queue as pq
    pq._INITIALIZED_PATHS.clear()
    yield
    pq._INITIALIZED_PATHS.clear()


def _payload(**kw):
    base = {"action": "add", "target": "memory", "content": "x"}
    base.update(kw)
    return base


# ===========================================================================
# Crash-safe initialization / reopen
# ===========================================================================

class TestInitAndReopen:
    def test_db_file_created_with_wal_and_restrictive_permissions(self, hermes_home):
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("overflow", "add", "memory", _payload())
        assert rec["status"] == "pending"

        db_path = pq.queue_db_path()
        assert db_path.exists()
        assert db_path.stat().st_mode & 0o777 == 0o600
        assert db_path.parent.stat().st_mode & 0o777 == 0o700

        conn = sqlite3.connect(str(db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() in ("wal", "delete")  # delete = documented NFS/host fallback
        finally:
            conn.close()

    def test_reopen_after_simulated_process_restart_sees_prior_data(self, hermes_home):
        """Each public call opens+closes its own connection (no long-lived
        handle), so a fresh call after clearing the in-process init cache is
        equivalent to a new process reopening the file after a crash."""
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("overflow", "add", "memory", _payload(content="durable fact"))

        # Simulate "process restarted": drop the in-process schema-init cache.
        pq._INITIALIZED_PATHS.clear()

        reloaded = pq.get(rec["id"])
        assert reloaded is not None
        assert reloaded["payload"]["content"] == "durable fact"
        assert reloaded["status"] == "pending"

    def test_schema_init_is_idempotent_across_repeated_connects(self, hermes_home):
        import tools.memory_pending_queue as pq

        for _ in range(5):
            pq.enqueue("overflow", "add", "memory", _payload(content=f"fact-{_}"))
        assert pq.count_active() == 5


# ===========================================================================
# Idempotent enqueue
# ===========================================================================

class TestEnqueueIdempotency:
    def test_duplicate_enqueue_returns_same_record(self, hermes_home):
        import tools.memory_pending_queue as pq

        p = _payload(content="remember this")
        first = pq.enqueue("overflow", "add", "memory", p, summary="s1")
        second = pq.enqueue("overflow", "add", "memory", p, summary="s2")

        assert first["id"] == second["id"]
        assert pq.count_active() == 1

    def test_different_payload_creates_distinct_record(self, hermes_home):
        import tools.memory_pending_queue as pq

        a = pq.enqueue("overflow", "add", "memory", _payload(content="fact A"))
        b = pq.enqueue("overflow", "add", "memory", _payload(content="fact B"))

        assert a["id"] != b["id"]
        assert pq.count_active() == 2

    def test_duplicate_after_resolution_is_not_deduped(self, hermes_home):
        """Once a record reaches a terminal status, re-enqueuing the same
        operation must create a NEW active record -- otherwise a fact that
        was already applied/discarded could never be queued again."""
        import tools.memory_pending_queue as pq

        p = _payload(content="recurring fact")
        first = pq.enqueue("overflow", "add", "memory", p)
        claimed = pq.claim_next("curator-1")
        assert claimed["id"] == first["id"]
        assert pq.mark_done(first["id"], "curator-1") is True

        second = pq.enqueue("overflow", "add", "memory", p)
        assert second["id"] != first["id"]
        assert pq.count_active() == 1


# ===========================================================================
# Concurrent claim / lease behavior
# ===========================================================================

class TestClaimAndLease:
    def test_concurrent_claims_never_double_claim(self, hermes_home):
        import tools.memory_pending_queue as pq

        ids = [
            pq.enqueue("overflow", "add", "memory", _payload(content=f"c{i}"))["id"]
            for i in range(8)
        ]

        claimed_ids = []
        lock = threading.Lock()
        errors = []

        def worker(owner):
            while True:
                rec = pq.claim_next(owner)
                if rec is None:
                    return
                with lock:
                    if rec["id"] in claimed_ids:
                        errors.append(f"double claim of {rec['id']} by {owner}")
                    claimed_ids.append(rec["id"])

        threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert sorted(claimed_ids) == sorted(ids)
        assert pq.count_active() == 8  # claimed -> 'processing', still active
        assert len(pq.list_active()) == 8

    def test_expired_lease_is_reclaimed(self, hermes_home):
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("overflow", "add", "memory", _payload())
        first = pq.claim_next("owner-A", lease_seconds=-1)  # already expired
        assert first["id"] == rec["id"]

        second = pq.claim_next("owner-B")
        assert second is not None
        assert second["id"] == rec["id"]
        assert second["lease_owner"] == "owner-B"

    def test_unexpired_lease_is_not_reclaimed(self, hermes_home):
        import tools.memory_pending_queue as pq

        pq.enqueue("overflow", "add", "memory", _payload())
        pq.claim_next("owner-A", lease_seconds=300)
        assert pq.claim_next("owner-B") is None

    def test_mark_done_requires_matching_lease_owner(self, hermes_home):
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("overflow", "add", "memory", _payload())
        pq.claim_next("owner-A")
        assert pq.mark_done(rec["id"], "owner-B") is False  # wrong owner
        assert pq.mark_done(rec["id"], "owner-A") is True
        assert pq.get(rec["id"])["status"] == "done"

    def test_mark_failed_retries_then_dead_letters(self, hermes_home):
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("overflow", "add", "memory", _payload())
        for _ in range(pq.DEFAULT_MAX_ATTEMPTS):
            claimed = pq.claim_next("curator")
            assert claimed is not None
            status = pq.mark_failed(claimed["id"], "curator", "boom", retry_delay_seconds=0)
            assert status in ("pending", "dead")

        final = pq.get(rec["id"])
        assert final["status"] == "dead"
        assert final["error_detail"] == "boom"

    def test_done_and_dead_records_are_never_deleted(self, hermes_home):
        """Completion/dead-lettering is a status transition, not an unlink --
        the record must remain retrievable for audit."""
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("overflow", "add", "memory", _payload())
        pq.claim_next("curator")
        pq.mark_done(rec["id"], "curator")
        assert pq.get(rec["id"]) is not None
        assert pq.get(rec["id"])["status"] == "done"
        assert rec["id"] not in [r["id"] for r in pq.list_active()]
        assert rec["id"] in [r["id"] for r in pq.list_all()]


# ===========================================================================
# Stale replace/remove conflict detection
# ===========================================================================

class TestStaleConflict:
    def test_matching_hash_is_not_stale(self, hermes_home):
        import tools.memory_pending_queue as pq

        h = pq.content_snapshot_hash("entry-one\n§\nentry-two")
        rec = pq.enqueue(
            "overflow", "replace", "memory",
            _payload(action="replace", old_text="entry-one", content="entry-one-v2"),
            expected_previous_hash=h,
        )
        assert pq.is_stale(rec, h) is False

    def test_changed_hash_is_stale(self, hermes_home):
        import tools.memory_pending_queue as pq

        h_at_enqueue = pq.content_snapshot_hash("entry-one\n§\nentry-two")
        h_now = pq.content_snapshot_hash("entry-one-EDITED\n§\nentry-two")
        rec = pq.enqueue(
            "overflow", "replace", "memory",
            _payload(action="replace", old_text="entry-one", content="entry-one-v2"),
            expected_previous_hash=h_at_enqueue,
        )
        assert pq.is_stale(rec, h_now) is True

    def test_add_only_record_has_no_staleness(self, hermes_home):
        """Pure appends never carry expected_previous_hash -- they can't clobber."""
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("overflow", "add", "memory", _payload())
        assert rec["expected_previous_hash"] is None
        assert pq.is_stale(rec, "anything") is False

    def test_mark_conflict_dead_letters_without_retry(self, hermes_home):
        import tools.memory_pending_queue as pq

        h = pq.content_snapshot_hash("v1")
        rec = pq.enqueue(
            "overflow", "replace", "memory",
            _payload(action="replace", old_text="a", content="b"),
            expected_previous_hash=h,
        )
        claimed = pq.claim_next("curator")
        assert pq.is_stale(claimed, pq.content_snapshot_hash("v2")) is True
        assert pq.mark_conflict(rec["id"], "curator", "stale: target changed since queued") is True

        final = pq.get(rec["id"])
        assert final["status"] == "dead"
        assert "stale" in final["error_detail"]
        # A dead conflict must not be silently retried into an auto-claim.
        assert pq.claim_next("curator-2") is None


# ===========================================================================
# Queue cap
# ===========================================================================

class TestQueueCap:
    def test_enqueue_raises_when_cap_reached(self, hermes_home):
        import tools.memory_pending_queue as pq

        for i in range(3):
            pq.enqueue("overflow", "add", "memory", _payload(content=f"c{i}"), cap=3)

        with pytest.raises(pq.QueueFullError):
            pq.enqueue("overflow", "add", "memory", _payload(content="one too many"), cap=3)

        assert pq.count_active() == 3

    def test_cap_counts_only_active_records(self, hermes_home):
        """A done/dead record must free up cap headroom -- the cap bounds the
        unresolved backlog, not lifetime history."""
        import tools.memory_pending_queue as pq

        recs = [
            pq.enqueue("overflow", "add", "memory", _payload(content=f"c{i}"), cap=2)
            for i in range(2)
        ]
        with pytest.raises(pq.QueueFullError):
            pq.enqueue("overflow", "add", "memory", _payload(content="blocked"), cap=2)

        claimed = pq.claim_next("curator")
        pq.mark_done(claimed["id"], "curator")

        # One slot freed -- this enqueue must now succeed.
        pq.enqueue("overflow", "add", "memory", _payload(content="fits now"), cap=2)
        assert pq.count_active() == 2

    def test_default_cap_is_conservative(self, hermes_home):
        import tools.memory_pending_queue as pq
        assert pq.DEFAULT_QUEUE_CAP == 500


# ===========================================================================
# Discard (approval-reject semantics)
# ===========================================================================

class TestDiscard:
    def test_discard_marks_dead_and_keeps_record(self, hermes_home):
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("approval", "add", "memory", _payload())
        assert pq.discard(rec["id"], reason="rejected by user") is True

        final = pq.get(rec["id"])
        assert final["status"] == "dead"
        assert final["error_detail"] == "rejected by user"
        assert rec["id"] not in [r["id"] for r in pq.list_active()]

    def test_discard_unknown_id_returns_false(self, hermes_home):
        import tools.memory_pending_queue as pq
        assert pq.discard("does-not-exist") is False

    def test_discard_already_terminal_record_returns_false(self, hermes_home):
        import tools.memory_pending_queue as pq

        rec = pq.enqueue("approval", "add", "memory", _payload())
        pq.claim_next("curator")
        pq.mark_done(rec["id"], "curator")
        assert pq.discard(rec["id"]) is False


# ===========================================================================
# import_legacy -- id-keyed idempotent insert used by legacy JSON migration
# ===========================================================================

class TestImportLegacy:
    def test_import_inserts_with_requested_id_and_preserves_created_at(self, hermes_home):
        import tools.memory_pending_queue as pq

        record, inserted = pq.import_legacy(
            "legacy-overflow-queued-123-456",
            pq.KIND_OVERFLOW, "add", "memory",
            _payload(content="migrated fact"),
            summary="overflow add on memory (migrated)",
            origin="foreground",
            created_at=1000.0,
        )
        assert inserted is True
        assert record["id"] == "legacy-overflow-queued-123-456"
        assert record["created_at"] == 1000.0
        assert record["status"] == "pending"
        assert pq.get("legacy-overflow-queued-123-456")["payload"]["content"] == "migrated fact"

    def test_reimport_same_id_is_idempotent_noop(self, hermes_home):
        import tools.memory_pending_queue as pq

        first, first_inserted = pq.import_legacy(
            "legacy-id-1", pq.KIND_OVERFLOW, "add", "memory",
            _payload(content="fact"), created_at=1000.0,
        )
        assert first_inserted is True

        second, second_inserted = pq.import_legacy(
            "legacy-id-1", pq.KIND_OVERFLOW, "add", "memory",
            _payload(content="fact"), created_at=1000.0,
        )
        assert second_inserted is False
        assert second["id"] == first["id"]
        assert pq.count_active() == 1

    def test_reimport_after_resolution_is_still_idempotent_on_id(self, hermes_home):
        """Unlike enqueue(), import_legacy dedupes on the deterministic legacy
        id even once the record reaches a terminal state -- re-running
        migration against a not-yet-archived file must never create a
        second row for the same original record."""
        import tools.memory_pending_queue as pq

        rec, _ = pq.import_legacy(
            "legacy-id-2", pq.KIND_OVERFLOW, "add", "memory", _payload(),
        )
        pq.claim_next("curator")
        pq.mark_done(rec["id"], "curator")

        again, inserted = pq.import_legacy(
            "legacy-id-2", pq.KIND_OVERFLOW, "add", "memory", _payload(),
        )
        assert inserted is False
        assert again["id"] == "legacy-id-2"
        assert again["status"] == "done"
        assert pq.count_active() == 0

    def test_import_dedupes_against_live_content_hash_when_id_differs(self, hermes_home):
        """If the same logical operation was already enqueued live (e.g. the
        upgraded code ran once before migration caught up), importing the
        legacy copy under a different id must not duplicate it."""
        import tools.memory_pending_queue as pq

        live = pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content="dup"))
        imported, inserted = pq.import_legacy(
            "legacy-different-id", pq.KIND_OVERFLOW, "add", "memory",
            _payload(content="dup"),
        )
        assert inserted is False
        assert imported["id"] == live["id"]
        assert pq.count_active() == 1

    def test_import_raises_queue_full_without_inserting(self, hermes_home):
        import tools.memory_pending_queue as pq

        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content="a"), cap=1)
        with pytest.raises(pq.QueueFullError):
            pq.import_legacy(
                "legacy-id-3", pq.KIND_OVERFLOW, "add", "memory",
                _payload(content="b"), cap=1,
            )
        assert pq.get("legacy-id-3") is None

    def test_import_preserves_expected_previous_hash(self, hermes_home):
        import tools.memory_pending_queue as pq

        h = pq.content_snapshot_hash("v1")
        rec, _ = pq.import_legacy(
            "legacy-id-4", pq.KIND_APPROVAL, "replace", "memory",
            _payload(action="replace", old_text="a", content="b"),
            expected_previous_hash=h,
        )
        assert rec["expected_previous_hash"] == h
