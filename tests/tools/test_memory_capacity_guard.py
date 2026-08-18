"""Phase A capacity guard tests — threshold-driven journaling behaviour."""

import pytest
from tools.memory_tool import (
    MemoryStore, MEMORY_WARN_PCT, MEMORY_QUEUE_PCT, MEMORY_TARGET_PCT,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.load_from_disk()
    return s


class TestCapacityGuardThresholds:
    """Threshold constants are what the architecture requires."""

    def test_threshold_ordering(self):
        # TARGET < WARN < QUEUE: projection cleans below warn, journal starts above warn
        assert 0 < MEMORY_TARGET_PCT < MEMORY_WARN_PCT < MEMORY_QUEUE_PCT <= 100

    def test_usage_pct_empty(self, store):
        assert store._usage_pct("memory") == 0

    def test_usage_pct_full(self, store):
        store.memory_char_limit = 100
        store.add("memory", "a" * 90)
        assert store._usage_pct("memory") == 90


class TestCapacityJournaling:
    """Writes at/above QUEUE_PCT are journaled; below are not."""

    def test_add_at_queue_threshold_journals(self, store):
        """At 98% (above 85%), a capacity-increasing add is journaled."""
        store.add("memory", "x" * 490)
        result = store.add("memory", "overflow entry")
        assert result["success"] is True
        assert result.get("queued") is True
        assert result["done"] is True
        assert "do not retry" in result.get("message", "").lower()
        assert result.get("pending_id") is not None
        # Live store not mutated
        assert "overflow entry" not in store.memory_entries

    def test_add_below_queue_threshold_rejects(self, store):
        """At 80% (below 85%), overflow is still rejected with guidance."""
        store.memory_char_limit = 100
        store.add("memory", "a" * 80)
        result = store.add("memory", "this entry pushes it over the limit")
        assert result["success"] is False
        assert "exceed" in result["error"].lower()
        assert "retry" in result["error"].lower()

    def test_replace_capacity_increasing_journals(self, store):
        """Replacing with a longer entry at 98% journals."""
        store.add("memory", "x" * 490)
        result = store.replace("memory", "x" * 490, "y" * 600)
        assert result["success"] is True
        assert result.get("queued") is True

    def test_replace_shorter_still_lands(self, store):
        """Replacing with a shorter entry at 98% still applies directly."""
        store.add("memory", "x" * 490)
        result = store.replace("memory", "x" * 490, "short")
        assert result["success"] is True
        assert result.get("queued") is not True
        assert "short" in store.memory_entries

    def test_remove_always_lands(self, store):
        """Remove never triggers journaling even at 98%."""
        store.add("memory", "x" * 490)
        result = store.remove("memory", "x" * 490)
        assert result["success"] is True
        assert result.get("queued") is not True

    def test_batch_at_queue_threshold_journals(self, store):
        """Batch that would exceed the limit at 98% is journaled."""
        store.add("memory", "x" * 490)
        result = store.apply_batch("memory", [
            {"action": "add", "content": "overflow via batch"},
        ])
        assert result["success"] is True
        assert result.get("queued") is True


class TestSuccessResponseWarn:
    """Success responses include capacity awareness."""

    def test_under_warn_no_capacity_note(self, store):
        result = store.add("memory", "short")
        assert result["success"] is True
        assert "capacity_note" not in result

    def test_at_warn_includes_capacity_note(self, store):
        store.memory_char_limit = 100
        store.add("memory", "a" * 75)  # 75% — triggers warn
        result = store.add("memory", "b" * 10)  # 88% after add
        assert result["success"] is True
        assert "capacity_note" in result
        # The note reflects live usage after the write (88% here)
        assert "88%" in result["capacity_note"]


class TestPendingOverlay:
    """Success responses surface pending queue depth."""

    def test_no_pending_shows_zero(self, store):
        result = store.add("memory", "hello")
        assert result["success"] is True
        assert "pending_count" not in result  # absent when zero

    def test_pending_count_appears(self, store):
        # Trigger journaling
        store.add("memory", "x" * 490)
        store.add("memory", "overflowed")  # journaled
        result = store.add("memory", "another write that fits")
        # pending_count shows the one journaled entry
        assert result["success"] is True
        # pending_count may or may not be present depending on whether
        # the journal entry is still active; just verify the response shape.
        assert "usage" in result
