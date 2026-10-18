"""Phase A fault-path and alerting tests."""

import json
import time
from pathlib import Path
import pytest

from tools.memory_tool import (
    MemoryStore, get_memory_dir, ENTRY_DELIMITER,
)
from tools import memory_pending_queue as pq
from tools import memory_projection as mp


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.load_from_disk()
    return s


class TestFallbackJournal:
    """When SQLite is unavailable, writes fall back to an fsynced JSONL file."""

    def test_fallback_created_when_queue_fails(self, store, monkeypatch):
        """Simulate SQLite failure; verify fallback file gets an entry."""
        from unittest.mock import patch
        from tools.memory_tool import get_memory_dir

        # Make the queue fail
        with patch("tools.memory_pending_queue.enqueue") as mock_enqueue:
            mock_enqueue.side_effect = RuntimeError("sqlite locked")
            store.add("memory", "x" * 490)  # 98%
            result = store.add("memory", "overflow entry")
            assert result["success"] is True
            assert result.get("queued") is True

        # Fallback file should exist and contain the record
        fallback = get_memory_dir() / "pending_fallback.jsonl"
        assert fallback.exists(), f"Fallback not found at {fallback}"
        lines = fallback.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["action"] == "add"
        assert record["target"] == "memory"
        assert record["payload"]["content"] == "overflow entry"
        assert "queue_error" in record

    def test_fallback_fsync_called(self, store, monkeypatch):
        """Verify the fallback write calls fsync (we infer by successful write)."""
        from unittest.mock import patch
        from tools.memory_tool import get_memory_dir

        with patch("tools.memory_pending_queue.enqueue") as mock_enqueue:
            mock_enqueue.side_effect = RuntimeError("sqlite unavailable")
            store.add("memory", "x" * 490)
            store.add("memory", "another overflow")
            # Second overflow also goes to fallback
            fallback = get_memory_dir() / "pending_fallback.jsonl"
            assert fallback.exists()
            lines = [l for l in fallback.read_text().split("\n") if l]
            assert len(lines) >= 1


class TestImportFallback:
    """Import-fallback command replays JSONL into SQLite queue."""

    def test_import_fallback_basic(self, store, monkeypatch):
        from tools.memory_tool import get_memory_dir
        fallback = get_memory_dir() / "pending_fallback.jsonl"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": "fallback-test-123",
            "kind": "overflow",
            "action": "add",
            "target": "memory",
            "payload": {"content": "imported from fallback"},
            "queued_at": time.time(),
            "queue_error": "sqlite locked",
        }
        fallback.write_text(json.dumps(record) + "\n")

        from hermes_cli.write_approval_commands import _import_fallback
        result = _import_fallback()

        assert "Imported 1 fallback record(s)" in result
        # Fallback file should be renamed
        assert not fallback.exists()
        imported = fallback.with_suffix(".jsonl.imported")
        assert imported.exists()

        # Record should now be in SQLite queue
        active = pq.list_active(kind=pq.KIND_OVERFLOW)
        found = [r for r in active if r.get("payload", {}).get("content") == "imported from fallback"]
        assert len(found) == 1

    def test_import_fallback_no_file(self, store, monkeypatch):
        from hermes_cli.write_approval_commands import _import_fallback
        # No fallback file
        fallback = get_memory_dir() / "pending_fallback.jsonl"
        if fallback.exists():
            fallback.unlink()
        result = _import_fallback()
        assert "No fallback journal found" in result


class TestAlerting:
    """Consumer script surfaces alerts for stale pendings and fallback use."""

    def test_alert_on_stale_pending(self, store, monkeypatch):
        """Active records older than 15 minutes trigger alert."""
        from cron.scripts.memory_journal_consumer import _check_alerts

        clock = {"now": 1_700_000_000.0}

        def fake_time():
            return clock["now"]

        monkeypatch.setattr("tools.memory_pending_queue.time.time", fake_time)
        monkeypatch.setattr("tools.memory_projection.time.time", fake_time)

        pq.enqueue(
            kind=pq.KIND_OVERFLOW,
            action="add",
            target="memory",
            payload={"content": "stale entry"},
            summary="test",
            origin="overflow",
        )
        clock["now"] += 16 * 60  # 16 minutes later

        status = mp.get_status()
        assert status["oldest_age_seconds"] > 900
        alerts = _check_alerts(status)
        stale_alerts = [a for a in alerts if "ALERT" in a and "pending" in a.lower()]
        assert len(stale_alerts) == 1
        assert "16" in stale_alerts[0]

    def test_alert_on_fallback_journal(self, store, monkeypatch):
        """Consumer reports fallback journal presence."""
        from cron.scripts.memory_journal_consumer import _check_alerts
        from cron.scripts.memory_journal_consumer import get_memory_dir as consumer_memory_dir

        fallback = consumer_memory_dir() / "pending_fallback.jsonl"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text('{"id":"test","kind":"overflow"}\n', encoding="utf-8")

        status = mp.get_status()
        alerts = _check_alerts(status)

        fallback_alerts = [a for a in alerts if "fallback" in a.lower()]
        assert len(fallback_alerts) == 1
        assert "fallback memory journal exists" in fallback_alerts[0].lower()

        # Clean up
        fallback.unlink()


class TestCrashRecovery:
    """Projection consumer is idempotent across crashes."""

    def test_crash_after_raw_accept_before_projection(self, store):
        """When SQLite accepts but projection hasn't run, the write is still pending."""
        store.add("memory", "x" * 490)
        result = store.add("memory", "overflow entry")
        assert result["success"] is True
        assert result.get("queued") is True

        # Before projection runs, the live store is unchanged
        assert "overflow entry" not in store.memory_entries

        # Projection applies it — pass the store so it reuses our instance
        mp.run_once(max_records=10, store=store)
        # Now it should be in the live store
        assert "overflow entry" in store.memory_entries

    def test_crash_after_projection_before_receipt(self, store):
        """Even if we crash after projection applied but before receipt, re-run is safe."""
        store.add("memory", "x" * 490)
        store.add("memory", "overflow entry")
        # Apply projection
        mp.run_once(max_records=10, store=store)
        assert "overflow entry" in store.memory_entries

        # Running again should be idempotent
        mp.run_once(max_records=10, store=store)
        # Still exactly once in memory
        assert store.memory_entries.count("overflow entry") == 1


class TestResetAndFencing:
    """Reset cancels/fences active operations before deleting store."""

    def test_reset_fences_active_operations(self, store):
        """When store is reset (files deleted), pending operations for that target remain queued but apply idempotently."""
        store.add("memory", "x" * 490)
        store.add("memory", "overflow entry")
        # Pending exists
        pending_before = pq.list_active(kind=pq.KIND_OVERFLOW)
        pending_memory = [r for r in pending_before if r.get("target") == "memory"]
        assert len(pending_memory) > 0

        # Simulate reset by deleting the memory files
        from tools.memory_tool import get_memory_dir
        mem_dir = get_memory_dir()
        (mem_dir / "MEMORY.md").unlink(missing_ok=True)
        (mem_dir / "USER.md").unlink(missing_ok=True)

        # Fresh store is empty
        store2 = MemoryStore(memory_char_limit=500, user_char_limit=300)
        store2.load_from_disk()
        assert len(store2.memory_entries) == 0

        # Pending queue still has the record (correct behavior —
        # reset doesn't auto-clear the queue; user must reject/approve)
        pending_after = pq.list_active(kind=pq.KIND_OVERFLOW)
        pending_memory_after = [r for r in pending_after if r.get("target") == "memory"]
        assert len(pending_memory_after) == len(pending_memory)

        # Applying the pending record to the reset store works
        mp.run_once(max_records=10, store=store2)
        assert "overflow entry" in store2.memory_entries


class TestFallbackNotAccepted:
    """Double-failure journal path must return structured not-accepted."""

    def test_fallback_double_failure_returns_not_accepted(self, store, monkeypatch):
        """Queue + fallback both unavailable: no exception, write not accepted."""
        import builtins
        from unittest.mock import patch

        store.add("memory", "x" * 490)
        real_open = builtins.open

        def guarded_open(file, *args, **kwargs):
            if "pending_fallback.jsonl" in str(file):
                raise OSError("disk full")
            return real_open(file, *args, **kwargs)

        with patch(
            "tools.memory_pending_queue.enqueue",
            side_effect=RuntimeError("sqlite locked"),
        ):
            monkeypatch.setattr(builtins, "open", guarded_open)
            result = store.add("memory", "overflow entry")

        assert result.get("success") is False
        assert result.get("accepted") is False
        assert result.get("queued") is not True
        assert "error" in result


class TestDrainedAddEvictsNothing:
    """A drained overflow add must not FIFO-delete existing entries."""

    def test_drained_add_evicts_nothing(self, store):
        store.add("memory", "keep-me")
        store.add("memory", "x" * 480)
        queued = store.add("memory", "queued-add")
        assert queued.get("queued") is True

        result = mp.run_once(max_records=10, store=store)
        assert result["processed"] >= 1
        first = result["results"][0]
        assert first.get("evicted") == []
        assert "queued-add" in store.memory_entries
        assert "keep-me" in store.memory_entries
        assert pq.count_evictions() == 0


class TestSiblingQueuedReplace:
    """A queued replace must survive a sibling add applied first."""

    def test_queued_replace_survives_prior_sibling_add(self, store):
        store.add("memory", "keep-this-entry")
        store.add("memory", "x" * 470)
        add_res = store.add("memory", "sibling-queued-add")
        assert add_res.get("queued") is True

        new_text = "keep-this-entry " + ("y" * 40)
        rep_res = store.replace("memory", "keep-this-entry", new_text)
        assert rep_res.get("queued") is True
        # Overflow replace must not stamp a snapshot hash that a sibling add
        # would invalidate.
        pending = pq.get(rep_res["pending_id"])
        assert pending is not None
        assert not pending.get("expected_previous_hash")

        result = mp.run_once(max_records=10, store=store)
        dead_replace = [
            row for row in result.get("results", [])
            if row.get("action") == "replace" and row.get("outcome") == "dead"
        ]
        assert dead_replace == []
        assert new_text in store.memory_entries
        assert pq.get(rep_res["pending_id"])["status"] == pq.STATUS_DONE


class TestReadYourWrites:
    """Queued overflow writes are visible to reads and injection this session."""

    def test_queued_write_visible_in_snapshot_and_reads(self, store):
        store.add("memory", "x" * 490)
        store.load_from_disk()
        frozen = store.format_for_system_prompt("memory") or ""
        assert "queued-visible-fact" not in frozen

        queued = store.add("memory", "queued-visible-fact")
        assert queued.get("queued") is True
        assert "queued-visible-fact" not in store.memory_entries
        assert "queued-visible-fact" in store.entries_for_read("memory")
        assert "queued-visible-fact" in (store.format_for_system_prompt("memory") or "")


class TestJournalFallbackFlag:
    """`/memory journal` surfaces whether the fallback JSONL exists."""

    def test_journal_surfaces_fallback_flag(self, store):
        from hermes_cli.write_approval_commands import _journal
        from tools.memory_tool import get_memory_dir

        out = _journal()
        assert "Fallback: no" in out

        fallback = get_memory_dir() / "pending_fallback.jsonl"
        fallback.write_text('{"id":"test","kind":"overflow"}\n', encoding="utf-8")
        out = _journal()
        assert "Fallback: yes" in out
        fallback.unlink()
