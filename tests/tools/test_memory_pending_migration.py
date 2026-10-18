"""Tests for tools/memory_pending_migration.py -- one-time (idempotent, crash
safe) import of pre-SQLite-journal legacy pending JSON files into the shared
``memory_pending.db`` queue.

Two legacy locations existed before ``tools/memory_pending_queue.py``:
  * ``~/.hermes/memories/pending/*.json`` -- memory_tool overflow writes
  * ``~/.hermes/pending/memory/*.json``   -- write_approval staged writes

Uses a real temp HERMES_HOME and real sqlite connections throughout (per
AGENTS.md), never a mock.
"""

import json
import os
import shutil
import tempfile
import time

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_pqmig_test_")
    home = os.path.join(d, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_init_cache():
    import tools.memory_pending_queue as pq
    pq._INITIALIZED_PATHS.clear()
    pq._MIGRATED_PATHS.clear()
    yield
    pq._INITIALIZED_PATHS.clear()
    pq._MIGRATED_PATHS.clear()


def _overflow_dir(home):
    d = os.path.join(home, "memories", "pending")
    os.makedirs(d, exist_ok=True)
    return d


def _approval_dir(home):
    d = os.path.join(home, "pending", "memory")
    os.makedirs(d, exist_ok=True)
    return d


def _write_overflow_file(home, name, *, target="memory", operations=None, queued_at=1700000000.0):
    operations = operations or [{"action": "add", "content": "legacy overflow fact"}]
    path = os.path.join(_overflow_dir(home), name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "schema": 1,
            "queued_at": queued_at,
            "target": target,
            "operations": operations,
            "usage": {"current": 100, "limit": 200},
        }, f)
    return path


def _write_approval_file(home, name, *, action="add", target="memory", content="legacy staged fact",
                          summary="legacy staged fact", origin="foreground", created_at=1700000001.0,
                          legacy_id="abcd1234", expected_previous_hash=None):
    payload = {"action": action, "target": target, "content": content, "old_text": None}
    if expected_previous_hash:
        payload["expected_previous_hash"] = expected_previous_hash
    path = os.path.join(_approval_dir(home), name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "id": legacy_id,
            "subsystem": "memory",
            "action": action,
            "summary": summary,
            "origin": origin,
            "created_at": created_at,
            "payload": payload,
        }, f)
    return path


# ===========================================================================
# Basic import from both legacy locations
# ===========================================================================

class TestMigrateBothLocations:
    def test_migrates_overflow_file(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        _write_overflow_file(hermes_home, "queued-1700000000000000-111.json")
        result = mig.migrate_legacy_pending()

        assert len(result["migrated"]) == 1
        assert not result["failed"]
        active = pq.list_active(kind=pq.KIND_OVERFLOW)
        assert len(active) == 1
        assert active[0]["payload"]["content"] == "legacy overflow fact"
        assert active[0]["created_at"] == 1700000000.0
        assert active[0]["status"] == "pending"

    def test_migrates_approval_file(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        _write_approval_file(hermes_home, "abcd1234.json")
        result = mig.migrate_legacy_pending()

        assert len(result["migrated"]) == 1
        active = pq.list_active(kind=pq.KIND_APPROVAL)
        assert len(active) == 1
        assert active[0]["payload"]["content"] == "legacy staged fact"
        assert active[0]["summary"] == "legacy staged fact"
        assert active[0]["created_at"] == 1700000001.0

    def test_migrates_from_both_locations_in_one_pass(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        _write_overflow_file(hermes_home, "queued-1-1.json")
        _write_approval_file(hermes_home, "id1.json", legacy_id="id1")
        result = mig.migrate_legacy_pending()

        assert len(result["migrated"]) == 2
        assert pq.count_active() == 2

    def test_no_legacy_dirs_is_a_clean_noop(self, hermes_home):
        import tools.memory_pending_migration as mig

        result = mig.migrate_legacy_pending()
        assert result == {"migrated": [], "failed": []}

    def test_batch_operations_preserved(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        ops = [
            {"action": "remove", "old_text": "stale"},
            {"action": "add", "content": "new fact"},
        ]
        _write_overflow_file(hermes_home, "queued-batch-1.json", operations=ops)
        mig.migrate_legacy_pending()

        active = pq.list_active(kind=pq.KIND_OVERFLOW)
        assert active[0]["action"] == "batch"
        assert active[0]["payload"]["operations"] == ops


# ===========================================================================
# Idempotency / duplicate rerun
# ===========================================================================

class TestIdempotentRerun:
    def test_rerunning_migration_does_not_duplicate(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        _write_overflow_file(hermes_home, "queued-dup-1.json")
        mig.migrate_legacy_pending()
        mig.migrate_legacy_pending()
        mig.migrate_legacy_pending()

        assert pq.count_active() == 1

    def test_successfully_migrated_file_is_archived_not_deleted(self, hermes_home):
        import tools.memory_pending_migration as mig

        path = _write_overflow_file(hermes_home, "queued-arch-1.json")
        mig.migrate_legacy_pending()

        assert not os.path.exists(path)
        archived = os.path.join(os.path.dirname(path), "migrated", "queued-arch-1.json")
        assert os.path.exists(archived)

    def test_archived_file_content_is_unmodified(self, hermes_home):
        import tools.memory_pending_migration as mig

        path = _write_overflow_file(hermes_home, "queued-arch-2.json")
        with open(path, encoding="utf-8") as f:
            original = f.read()
        mig.migrate_legacy_pending()
        archived = os.path.join(os.path.dirname(path), "migrated", "queued-arch-2.json")
        with open(archived, encoding="utf-8") as f:
            assert f.read() == original


# ===========================================================================
# Malformed JSON / partial failure / retryability
# ===========================================================================

class TestMalformedAndPartialFailure:
    def test_malformed_json_is_reported_and_left_in_place(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        bad_path = os.path.join(_overflow_dir(hermes_home), "queued-broken.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        result = mig.migrate_legacy_pending()
        assert len(result["failed"]) == 1
        assert "queued-broken.json" in result["failed"][0]["file"]
        assert os.path.exists(bad_path)  # not archived, not deleted
        assert pq.count_active() == 0

    def test_malformed_file_is_retried_on_next_pass_after_fix(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        bad_path = os.path.join(_overflow_dir(hermes_home), "queued-fixable.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        result1 = mig.migrate_legacy_pending()
        assert len(result1["failed"]) == 1

        # "operator" fixes the file by hand
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump({
                "schema": 1, "queued_at": 1.0, "target": "memory",
                "operations": [{"action": "add", "content": "now valid"}],
            }, f)
        result2 = mig.migrate_legacy_pending()
        assert len(result2["migrated"]) == 1
        assert pq.count_active() == 1

    def test_one_bad_file_does_not_block_other_valid_files(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        bad_path = os.path.join(_overflow_dir(hermes_home), "queued-bad.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("not json at all {{{")
        _write_overflow_file(hermes_home, "queued-good.json")

        result = mig.migrate_legacy_pending()
        assert len(result["migrated"]) == 1
        assert len(result["failed"]) == 1
        assert pq.count_active() == 1

    def test_missing_required_fields_reported_as_failure(self, hermes_home):
        import tools.memory_pending_migration as mig

        path = os.path.join(_overflow_dir(hermes_home), "queued-incomplete.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "queued_at": 1.0}, f)  # missing target/operations

        result = mig.migrate_legacy_pending()
        assert len(result["failed"]) == 1
        assert os.path.exists(path)

    def test_approval_file_missing_payload_reported_as_failure(self, hermes_home):
        import tools.memory_pending_migration as mig

        path = os.path.join(_approval_dir(hermes_home), "broken.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"id": "x", "subsystem": "memory"}, f)  # no payload

        result = mig.migrate_legacy_pending()
        assert len(result["failed"]) == 1
        assert os.path.exists(path)

    def test_queue_full_leaves_file_unarchived_and_retryable(self, hermes_home):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", {"content": "filler"}, cap=1)
        path = _write_overflow_file(hermes_home, "queued-blocked.json")

        result = mig.migrate_legacy_pending(cap=1)
        assert len(result["failed"]) == 1
        assert result["failed"][0].get("retryable") is True
        assert os.path.exists(path)  # left for retry, not archived

        # free up cap, then retry succeeds
        claimed = pq.claim_next("curator")
        pq.mark_done(claimed["id"], "curator")
        result2 = mig.migrate_legacy_pending(cap=1)
        assert len(result2["migrated"]) == 1
        assert not os.path.exists(path)


# ===========================================================================
# Rollback / crash recovery
# ===========================================================================

class TestCrashRecovery:
    def test_file_left_in_place_when_import_raises_unexpectedly(self, hermes_home, monkeypatch):
        """Simulates a crash between reading the file and the DB commit: the
        row must not exist half-written, and the file must remain for retry."""
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        path = _write_overflow_file(hermes_home, "queued-crash.json")

        def _boom(*a, **kw):
            raise RuntimeError("simulated crash mid-import")

        monkeypatch.setattr(pq, "import_legacy", _boom)
        result = mig.migrate_legacy_pending()

        assert len(result["failed"]) == 1
        assert os.path.exists(path)
        assert pq.count_active() == 0

    def test_recovers_and_completes_after_transient_failure(self, hermes_home, monkeypatch):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        path = _write_overflow_file(hermes_home, "queued-recover.json")

        real_import = pq.import_legacy
        calls = {"n": 0}

        def _flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return real_import(*a, **kw)

        monkeypatch.setattr(pq, "import_legacy", _flaky)
        result1 = mig.migrate_legacy_pending()
        assert len(result1["failed"]) == 1
        assert os.path.exists(path)

        result2 = mig.migrate_legacy_pending()
        assert len(result2["migrated"]) == 1
        assert not os.path.exists(path)

    def test_crash_between_insert_and_archive_is_safe_on_retry(self, hermes_home, monkeypatch):
        """The row is committed durably before the file is archived. If the
        process dies right after commit but before the rename, the next run
        must not duplicate the row -- it re-imports the same id (a no-op)
        and then completes the archive."""
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        path = _write_overflow_file(hermes_home, "queued-halfway.json")

        real_archive = mig._archive
        monkeypatch.setattr(mig, "_archive", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            mig.migrate_legacy_pending()

        assert pq.count_active() == 1  # row committed
        assert os.path.exists(path)  # archive step never completed

        monkeypatch.setattr(mig, "_archive", real_archive)
        result = mig.migrate_legacy_pending()
        assert pq.count_active() == 1  # still just one row -- no duplicate
        assert not os.path.exists(path)
        assert result["migrated"][0]["inserted"] is False


# ===========================================================================
# Concurrency: two processes/threads racing the same legacy file
# ===========================================================================

class TestConcurrentMigration:
    def test_concurrent_migration_passes_do_not_duplicate(self, hermes_home):
        import threading
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        for i in range(5):
            _write_overflow_file(
                hermes_home,
                f"queued-conc-{i}.json",
                operations=[{"action": "add", "content": f"legacy concurrent fact {i}"}],
            )

        errors = []

        def worker():
            try:
                mig.migrate_legacy_pending()
            except Exception as e:  # pragma: no cover - surfaced via errors list
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert pq.count_active() == 5


# ===========================================================================
# First-open auto-migration hook (integration with memory_pending_queue)
# ===========================================================================

class TestAutoMigrationOnFirstOpen:
    def test_first_enqueue_call_triggers_migration(self, hermes_home):
        import tools.memory_pending_queue as pq

        _write_overflow_file(hermes_home, "queued-auto-1.json")
        # No explicit migrate_legacy_pending() call -- just touching the queue.
        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", {"content": "live write"})

        all_active = pq.list_active()
        contents = [r["payload"].get("content") for r in all_active]
        assert "legacy overflow fact" in contents
        assert "live write" in contents

    def test_migration_runs_only_once_per_process_path(self, hermes_home, monkeypatch):
        import tools.memory_pending_migration as mig
        import tools.memory_pending_queue as pq

        _write_overflow_file(hermes_home, "queued-auto-2.json")

        calls = {"n": 0}
        real = mig.migrate_legacy_pending

        def _counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        monkeypatch.setattr(mig, "migrate_legacy_pending", _counting)

        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", {"content": "a"})
        pq.enqueue(pq.KIND_OVERFLOW, "add", "memory", {"content": "b"})
        pq.list_active()
        pq.get("does-not-exist")

        assert calls["n"] == 1

    def test_write_approval_list_pending_sees_migrated_records(self, hermes_home):
        """/memory pending is backed by write_approval.list_pending('memory'),
        which must see records that only existed as legacy JSON on disk."""
        from tools import write_approval as wa

        _write_approval_file(hermes_home, "legacy-appr.json", legacy_id="legacy-appr",
                              content="visible via /memory pending")

        records = wa.list_pending(wa.MEMORY)
        summaries = [r["payload"].get("content") for r in records]
        assert "visible via /memory pending" in summaries
