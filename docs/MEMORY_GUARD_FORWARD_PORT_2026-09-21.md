# Memory capacity guard forward-port (2026-09-21, upstream v2026.9.14)

**Goal.** Stop `tools/memory_tool.py` conflicting on every upstream merge. Upstream split
`MemoryStore` into `tools/memory_tool_store.py`; the fork had kept its 2103-line `memory_tool.py`
("ours wholesale") and hand-reverse-ported five upstream fixes. Now both upstream files are
upstream's, and the fork's guard is an isolated fork-only layer.

## Final layout

| File | Lines | vs upstream v2026.9.14 |
|---|---|---|
| `tools/memory_tool_store.py` | 440 | byte-identical (0 lines differ), **no hooks** |
| `tools/memory_tool.py` | 403 (upstream 397) | 7 added / 1 changed — the 2 hooks below |
| `tools/memory_capacity_guard.py` (new, fork-only) | 807 | n/a — never conflicts |
| `tests/tools/test_memory_capacity_guard_layer.py` (new) | 38 | fails if a merge drops a hook / re-exports |

`tools/memory_capacity_guard.py` holds: thresholds (`MEMORY_WARN_PCT` 75 / `MEMORY_QUEUE_PCT` 85 /
`MEMORY_TARGET_PCT` 70), pin + consolidation helpers (`PIN_MARKER`, `is_pinned`,
`target_char_budget`, `entries_char_total`, `consolidate_entries`, ...), and
`GuardedMemoryStore(MemoryStore)`: hysteresis gate, SQLite-queue journal + fsynced JSONL fallback,
pending overlay (`entries_for_read`, overlaid `format_for_system_prompt`), `capacity_consolidator`
hook, `apply_with_capacity` (projection consumer entry point), `_batch_error`, and the
`pending_count` / `capacity_note` additions to success responses.

### How it hooks upstream (no edits inside `MemoryStore` methods)

Upstream funnels every mutation through `MemoryStore._mutate(target, mutate, skip_drift)` where
`mutate(entries, limit)` returns a new-entries tuple or an error dict. The subclass:

1. `add` / `_edit` (replace only; remove is never journaled) / `apply_batch` are overridden only to
   record *which* operation is running in a `ContextVar` (`_GUARD_OP`; thread/async safe), then call
   `super()`.
2. `_mutate` wraps the caller's `mutate` callback. If upstream's own validation returned a failure
   AND `_would_overflow()` (replays the op on a copy with upstream's `_apply_batch_op`, so malformed /
   unmatched / ambiguous ops never count as overflow; a replace only counts if it grows the entry) AND
   the hysteresis gate says journal, the failure is replaced by the journaled `queued` result and the
   per-turn consolidation-failure counter bump is undone (parity with the old inline code). Otherwise
   any `current_entries` in the failure is replaced by the overlay view.
3. `_usage_pct(target)` (fork API, int, used by the guard and its tests) coexists with upstream's
   `_usage_pct(target, current)` (string) via an optional second argument.
4. `load_from_disk` / `_reload_target` / `save_to_disk` are thin re-implementations on upstream's
   primitives (`_read_raw_checked`, `_detect_external_drift`, `_write_file`) because the projection
   consumer's `apply_with_capacity` was written against them.

## Hook list (grep: `grep -rn "FORK(memory-capacity-guard)" tools/`)

| # | Location | What |
|---|---|---|
| 1 | `tools/memory_tool.py:45` (4 lines, right after the upstream store import) | rebinds `MemoryStore` to `GuardedMemoryStore` and re-exports `MEMORY_WARN_PCT/QUEUE_PCT/TARGET_PCT`, `PIN_MARKER`, `consolidate_entries`, `entries_char_total`, `is_pinned`, `target_char_budget` (operator scripts / `cron/scripts/memory_journal_consumer.py` / `tools/memory_projection.py` import them from `tools.memory_tool`) |
| 2 | `tools/memory_tool.py:128` (1 line in `_validate_single_op`) | `store.entries_for_read(target)` instead of `store._entries_for(target)` so a missing-`old_text` reply shows queued writes |

If upstream renames `_mutate`/`_edit`/`_apply_batch_op`/`_find_unique_match`/`_success_response`, the
fix is inside `tools/memory_capacity_guard.py` only.

## Hunk classification (old fork `memory_tool.py` 2103 lines vs upstream v2026.9.14)

Derived from `git diff fcbd1076a9 11e6f40d9a -- tools/memory_tool.py` (fork delta over upstream
v2026.8.19, +740/-31) plus the merge-time reverse-ports and the compaction drift.

**(a) Fork feature — moved to `tools/memory_capacity_guard.py`:** thresholds + `_MIN_CONSOLIDATED_ENTRY_CHARS`;
`PIN_MARKER`/`is_pinned`/`target_char_budget`/`entries_char_total`/`_summarize_entry_text`/
`_near_duplicate_pair`/`_merge_near_duplicate`/`_durable_facts_preserved`/`consolidate_entries`;
`_snapshot_entries` + `capacity_consolidator` attrs; `_usage_pct` int; `_should_journal_capacity_write`,
`_journal_capacity_write`, `_journal_fallback`, `_fallback_pending_for_target`, `_list_pending_for_target`,
`_apply_overlay_op`, `_apply_pending_overlay`, `entries_for_read`; the journal branches in
`add`/`replace`/`apply_batch`; `current_entries` overlay in every error path (`add`, `replace`, `remove`
no-match, `_batch_error`, `_missing_old_text_error`); `apply_with_capacity`; `_batch_error`;
overlaid `format_for_system_prompt`; `pending_count`/`capacity_note` in `_success_response`.

**(b) Reverse-ported upstream fixes — now upstream's own code, nothing to maintain:** secure 0600 /
`O_NOFOLLOW` lock files; oversize-on-load warning (#10877); empty-store batch refusal (#103419);
background-review delete gate (#105921, `_background_delete_gate`); PEP 562 plugin-compat shim.

**(c) Accidental drift (fork copy was pre-compaction upstream) — dropped, upstream's version now:**
un-split `MemoryStore` inside `memory_tool.py`; verbose docstrings; `_apply_write_gate` /
`_apply_batch_write_gate` pair (now `_gate_or_stage` + `_STORE_ACTIONS`); `_missing_old_text_error`
(now `_validate_single_op`); long-form `memory_tool()`; `_build_memory_schema_overrides` /
`_SINGLE_TARGET_TEXT`; `_reload_target`/`save_to_disk`/`_read_entries_checked`/`_previews`
(upstream removed them; the two the guard needs are re-implemented in the guard module).
**One user-visible drift item:** the `memory` tool's schema `WHEN:` paragraph is now upstream's
(task-learned knowledge goes to skills; commit 562ee8ab76) — the fork copy still carried the older
"save proactively" wording.

## Verification (2026-09-21)

Behaviour was compared against pre-change fork `main` with a scripted scenario (add at 80% rejected
with guidance, 98% add / growing replace / overflow batch journaled, failure counter unchanged,
overlay reads + overlaid system prompt, shrinking replace lands live, malformed batch still a normal
error, projection drain, warn note): full JSON output **identical** modulo ids. Tests: 34 memory /
guard / write-approval / consolidation-touching files 996 passed before and after; 24 more
neighbouring files 695 passed / 8 skipped; new layer test 4 passed.

## Known deltas / caveats

- `_would_overflow` replays the op with upstream's `_apply_batch_op`; if a future upstream
  `_apply_batch_op` signature changes, `test_memory_capacity_guard.py` fails first.
- `tests/tools/test_memory_capacity_guard_layer.py` deliberately greps for the hook tags.
- **Test-order hazard (upstream's, not the guard's):** `tests/tools/test_memory_tool_import_fallback.py`
  re-imports `tools.memory_tool` without fcntl and only `sys.modules` is restored afterwards, not the
  `tools.memory_tool` package attribute; any later test in the same process that uses
  `MemoryStore._file_lock` then sees a no-lock module, so `TestMemoryFileLockPermissions` (3 tests)
  fails. Reproduced on a pristine v2026.9.14 tree (`pytest test_memory_tool_import_fallback.py
  test_memory_tool.py`). Default collection order (`test_memory_tool.py` first) is fine; a
  locale-sorted file list that puts `_import_fallback` first is not. Sort with `LC_ALL=C`.
