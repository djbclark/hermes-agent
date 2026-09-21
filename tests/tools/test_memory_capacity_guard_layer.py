"""Structure tests for the fork-owned memory capacity guard layer.

``tools/memory_tool.py`` / ``tools/memory_tool_store.py`` track upstream verbatim;
the guard lives in ``tools/memory_capacity_guard.py`` and is wired in by a couple of
``# FORK(memory-capacity-guard):`` hooks. These tests fail loudly if an upstream
merge drops a hook (behaviour tests live in test_memory_capacity_guard.py).
"""

from pathlib import Path

import tools.memory_tool as mt
import tools.memory_tool_store as mts
from tools import memory_capacity_guard as guard

TAG = "FORK(memory-capacity-guard)"


def test_memory_store_is_the_guarded_subclass():
    assert issubclass(mt.MemoryStore, mts.MemoryStore)
    assert mt.MemoryStore is guard.GuardedMemoryStore


def test_guard_api_reexported_from_memory_tool():
    for name in ("MEMORY_WARN_PCT", "MEMORY_QUEUE_PCT", "MEMORY_TARGET_PCT", "PIN_MARKER",
                 "is_pinned", "target_char_budget", "entries_char_total", "consolidate_entries"):
        assert getattr(mt, name) is getattr(guard, name)


def test_hook_tags_present_in_memory_tool():
    text = Path(mt.__file__).read_text(encoding="utf-8")
    assert text.count(TAG) >= 2  # store rebind + entries_for_read in _validate_single_op
    assert "entries_for_read" in text


def test_upstream_store_module_has_no_guard_code():
    text = Path(mts.__file__).read_text(encoding="utf-8")
    assert TAG not in text
    assert "apply_with_capacity" not in text and "MEMORY_QUEUE_PCT" not in text
