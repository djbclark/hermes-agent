"""Tests for tools/memory_projection.py -- the deterministic (non-LLM)
journal consumer that claims/applies queued memory writes from the shared
SQLite pending-op journal (tools/memory_pending_queue.py).

Uses a real temp HERMES_HOME (matches the convention in
tests/tools/test_memory_pending_queue.py and test_memory_tool.py) so the
real SQLite + file-backed MemoryStore paths are exercised, not mocks.
"""

import os
import shutil
import tempfile
import threading
import time

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_proj_test_")
    home = os.path.join(d, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_init_cache():
    import tools.memory_pending_queue as pq
    pq._INITIALIZED_PATHS.clear()
    yield
    pq._INITIALIZED_PATHS.clear()


def _small_store():
    """A MemoryStore with a small char limit so overflow/eviction is cheap
    to trigger, sharing the real HERMES_HOME-backed files."""
    from tools.memory_tool import MemoryStore
    s = MemoryStore(memory_char_limit=120, user_char_limit=120)
    s.load_from_disk()
    return s


def _payload(**kw):
    base = {"action": "add", "target": "memory", "content": "x"}
    base.update(kw)
    return base


# ===========================================================================
# Duplicate-add idempotency
# ===========================================================================

class TestDuplicateAdd:
    def test_add_already_present_is_idempotent_done(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp
        from tools.memory_tool import MemoryStore

        store = MemoryStore(memory_char_limit=500, user_char_limit=500)
        store.load_from_disk()
        store.add("memory", "the fact")

        rec = pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content="the fact"))

        result = mp.claim_and_apply("consumer-1", store=store)
        assert result["outcome"] == "done"
        assert result["id"] == rec["id"]

        # Entry not duplicated on disk.
        fresh = MemoryStore(memory_char_limit=500, user_char_limit=500)
        fresh.load_from_disk()
        assert fresh.memory_entries.count("the fact") == 1

        resolved = pq.get(rec["id"])
        assert resolved["status"] == pq.STATUS_DONE

    def test_second_claim_of_replayed_add_is_still_idempotent(self, hermes_home):
        """A lease-expiry replay (record applied twice by two claimants)
        must not create a duplicate entry -- see crash/replay test below for
        the fuller scenario."""
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content="dup fact"))
        store = _small_store()

        first = mp.claim_and_apply("owner-a", store=store)
        assert first["outcome"] == "done"

        # Simulate a second, independent apply of the *same content* via a
        # freshly-enqueued record (e.g. the user also re-added it by hand).
        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content="dup fact"))
        second = mp.claim_and_apply("owner-b", store=store)
        assert second["outcome"] == "done"

        assert store.memory_entries.count("dup fact") == 1


# ===========================================================================
# Stale replace/remove rejection
# ===========================================================================

class TestStaleWrites:
    def test_stale_replace_is_dead_lettered_not_applied(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp
        from tools.memory_tool import MemoryStore, ENTRY_DELIMITER

        store = MemoryStore(memory_char_limit=500, user_char_limit=500)
        store.load_from_disk()
        store.add("memory", "original entry")

        stale_hash = pq.content_snapshot_hash(
            ENTRY_DELIMITER.join(store._entries_for("memory"))
        )

        # World changes after the snapshot was taken but before replay.
        store.add("memory", "an unrelated later fact")

        rec = pq.enqueue(
            pq.KIND_OVERFLOW, "replace", "memory",
            {"action": "replace", "target": "memory",
             "old_text": "original entry", "content": "replaced entry"},
            expected_previous_hash=stale_hash,
        )

        result = mp.claim_and_apply("consumer-1", store=store)
        assert result["outcome"] == "dead"
        assert "stale" in result["error"].lower()

        # Original entry untouched.
        assert "original entry" in store.memory_entries
        assert "replaced entry" not in store.memory_entries

        resolved = pq.get(rec["id"])
        assert resolved["status"] == pq.STATUS_DEAD

    def test_stale_remove_is_dead_lettered(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp
        from tools.memory_tool import MemoryStore, ENTRY_DELIMITER

        store = MemoryStore(memory_char_limit=500, user_char_limit=500)
        store.load_from_disk()
        store.add("memory", "entry to remove")

        stale_hash = pq.content_snapshot_hash(
            ENTRY_DELIMITER.join(store._entries_for("memory"))
        )
        store.add("memory", "something changed since")

        pq.enqueue(
            pq.KIND_OVERFLOW, "remove", "memory",
            {"action": "remove", "target": "memory", "old_text": "entry to remove"},
            expected_previous_hash=stale_hash,
        )

        result = mp.claim_and_apply("consumer-1", store=store)
        assert result["outcome"] == "dead"
        assert "entry to remove" in store.memory_entries

    def test_non_stale_replace_applies_normally(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp
        from tools.memory_tool import MemoryStore, ENTRY_DELIMITER

        store = MemoryStore(memory_char_limit=500, user_char_limit=500)
        store.load_from_disk()
        store.add("memory", "original entry")

        fresh_hash = pq.content_snapshot_hash(
            ENTRY_DELIMITER.join(store._entries_for("memory"))
        )
        pq.enqueue(
            pq.KIND_OVERFLOW, "replace", "memory",
            {"action": "replace", "target": "memory",
             "old_text": "original entry", "content": "replaced entry"},
            expected_previous_hash=fresh_hash,
        )

        result = mp.claim_and_apply("consumer-1", store=store)
        assert result["outcome"] == "done"
        assert "replaced entry" in store.memory_entries
        assert "original entry" not in store.memory_entries


# ===========================================================================
# Full-projection eviction
# ===========================================================================

class TestEviction:
    def test_overflow_evicts_oldest_unpinned_entry_instead_of_requeuing(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        store = _small_store()  # 120 char limit
        store.add("memory", "aaaaaaaaaaaaaaaaaaaa")  # oldest
        store.add("memory", "bbbbbbbbbbbbbbbbbbbb")

        big = "c" * 90  # forces overflow against the 120-char limit
        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content=big))

        active_before = pq.count_active()
        result = mp.claim_and_apply("consumer-1", store=store)

        assert result["outcome"] == "done"
        assert big in store.memory_entries
        # Oldest entry evicted, not the newer one.
        assert "aaaaaaaaaaaaaaaaaaaa" not in store.memory_entries
        assert result["evicted"] == ["aaaaaaaaaaaaaaaaaaaa"]

        # Never re-queued: no new active record was created for this write.
        assert pq.count_active() == active_before - 1

        # Eviction is archived, not silently dropped.
        evictions = pq.list_evictions(target="memory")
        assert len(evictions) == 1
        assert evictions[0]["entry_text"] == "aaaaaaaaaaaaaaaaaaaa"

    def test_pinned_entries_are_never_evicted(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp
        from tools.memory_tool import PIN_MARKER

        store = _small_store()
        pinned = f"{PIN_MARKER} important fact that must survive"
        store.add("memory", pinned)
        store.add("memory", "less important filler entry")

        big = "d" * 65
        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content=big))

        result = mp.claim_and_apply("consumer-1", store=store)

        assert pinned in store.memory_entries
        assert "less important filler entry" not in store.memory_entries
        assert result["outcome"] == "done"

    def test_all_entries_pinned_is_unresolvable_and_dead_lettered(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp
        from tools.memory_tool import PIN_MARKER

        store = _small_store()
        store.add("memory", f"{PIN_MARKER} entry one")
        store.add("memory", f"{PIN_MARKER} entry two")

        big = "e" * 90
        rec = pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content=big))

        result = mp.claim_and_apply("consumer-1", store=store)
        assert result["outcome"] == "dead"
        assert big not in store.memory_entries

        resolved = pq.get(rec["id"])
        assert resolved["status"] == pq.STATUS_DEAD


# ===========================================================================
# Lease expiry / retry / dead-letter
# ===========================================================================

class TestLeaseAndRetry:
    def test_expired_lease_is_reclaimed_by_another_consumer(self, hermes_home):
        import tools.memory_pending_queue as pq

        rec = pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content="claim me"))
        first = pq.claim_next("owner-a", lease_seconds=0.01)
        assert first["id"] == rec["id"]

        time.sleep(0.05)  # lease expires

        second = pq.claim_next("owner-b", lease_seconds=30)
        assert second is not None
        assert second["id"] == rec["id"]
        assert second["attempts"] == 2  # incremented on each claim

    def test_transient_failure_retries_then_dead_letters_after_max_attempts(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        store = _small_store()
        # old_text that will never match -- a permanent-shaped error routed
        # through the retry path, which must still terminate.
        rec = pq.enqueue(
            pq.KIND_OVERFLOW, "replace", "memory",
            {"action": "replace", "target": "memory",
             "old_text": "does not exist", "content": "irrelevant"},
        )

        last = None
        for _ in range(3):
            last = mp.claim_and_apply(
                "consumer-1", store=store, max_attempts=3, retry_delay_seconds=0,
            )
            assert last["outcome"] in ("retry", "dead")

        assert last["outcome"] == "dead"
        resolved = pq.get(rec["id"])
        assert resolved["status"] == pq.STATUS_DEAD
        assert resolved["attempts"] == 3
        assert resolved["error_detail"]

    def test_retryable_failure_stays_pending_before_max_attempts(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        store = _small_store()
        pq.enqueue(
            pq.KIND_OVERFLOW, "replace", "memory",
            {"action": "replace", "target": "memory",
             "old_text": "missing", "content": "x"},
        )

        result = mp.claim_and_apply(
            "consumer-1", store=store, max_attempts=5, retry_delay_seconds=0,
        )
        assert result["outcome"] == "retry"

        active = pq.list_active()
        assert len(active) == 1
        assert active[0]["status"] == pq.STATUS_PENDING
        assert active[0]["error_detail"]


# ===========================================================================
# Crash / replay
# ===========================================================================

class TestCrashReplay:
    def test_apply_landed_then_crash_before_ack_is_idempotent_on_replay(self, hermes_home):
        """Simulates: consumer applies the write (it lands on disk), then
        crashes before calling mark_done. The lease expires; a new consumer
        reclaims the record and must not double-apply."""
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        store = _small_store()
        rec = pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content="crash fact"))

        claimed = pq.claim_next("dead-owner", lease_seconds=0.01)
        assert claimed["id"] == rec["id"]
        # The write itself lands (this is what a real consumer would have
        # done before crashing)...
        store.add("memory", "crash fact")
        # ...but mark_done() is never called -- simulated crash.

        time.sleep(0.05)

        result = mp.claim_and_apply("survivor", store=store)
        assert result["outcome"] == "done"
        assert store.memory_entries.count("crash fact") == 1

        resolved = pq.get(rec["id"])
        assert resolved["status"] == pq.STATUS_DONE


# ===========================================================================
# Status reporting
# ===========================================================================

class TestStatus:
    def test_status_reports_bounded_fields(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        status = mp.get_status()
        assert status == {
            "active_count": 0,
            "pending_count": 0,
            "processing_count": 0,
            "failed_count": 0,
            "dead_letter_count": 0,
            "oldest_age_seconds": 0.0,
            "last_error": None,
            "last_error_record_id": None,
            "behind": False,
            "stale_after_seconds": mp.DEFAULT_STALE_AFTER_SECONDS,
        }

        pq.enqueue(
            pq.KIND_OVERFLOW, "replace", "memory",
            {"action": "replace", "target": "memory", "old_text": "missing", "content": "x"},
        )
        store = _small_store()
        mp.claim_and_apply("consumer-1", store=store, max_attempts=5, retry_delay_seconds=0)

        status = mp.get_status()
        assert status["active_count"] == 1
        assert status["failed_count"] == 0
        assert status["last_error"]
        assert status["oldest_age_seconds"] >= 0.0

    def test_status_does_not_expose_pending_content(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        pq.enqueue(
            pq.KIND_OVERFLOW, "add", "memory",
            _payload(content="SECRET-CONTENT-SHOULD-NOT-LEAK"),
        )
        status = mp.get_status()
        assert "SECRET-CONTENT-SHOULD-NOT-LEAK" not in repr(status)

    def test_behind_flag_true_when_failed_records_present(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        store = _small_store()
        pq.enqueue(
            pq.KIND_OVERFLOW, "replace", "memory",
            {"action": "replace", "target": "memory", "old_text": "missing", "content": "x"},
        )
        mp.claim_and_apply("consumer-1", store=store, max_attempts=5, retry_delay_seconds=0)
        assert mp.get_status()["behind"] is True


# ===========================================================================
# Concurrent consumers
# ===========================================================================

class TestConcurrentConsumers:
    def test_concurrent_consumers_apply_each_record_exactly_once(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        n = 12
        for i in range(n):
            pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content=f"concurrent fact {i}"))

        applied = []
        lock = threading.Lock()
        errors = []

        def worker(owner):
            while True:
                result = mp.claim_and_apply(owner, max_attempts=3, retry_delay_seconds=0)
                if result is None:
                    return
                with lock:
                    if result["id"] in applied:
                        errors.append(f"double apply of {result['id']} by {owner}")
                    applied.append(result["id"])

        threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert len(applied) == n

        from tools.memory_tool import MemoryStore
        final = MemoryStore(memory_char_limit=5000, user_char_limit=5000)
        final.load_from_disk()
        for i in range(n):
            assert f"concurrent fact {i}" in final.memory_entries
        # No duplicates from racing appliers.
        assert len(final.memory_entries) == len(set(final.memory_entries)) == n

    def test_run_once_drains_the_queue(self, hermes_home):
        import tools.memory_pending_queue as pq
        from tools import memory_projection as mp

        for i in range(5):
            pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", _payload(content=f"drain {i}"))

        result = mp.run_once(max_records=10)
        assert result["processed"] == 5
        assert result["counts"] == {"done": 5}
        assert pq.count_active() == 0
