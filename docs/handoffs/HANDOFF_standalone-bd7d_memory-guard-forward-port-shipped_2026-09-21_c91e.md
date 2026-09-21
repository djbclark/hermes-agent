---
schema_version: 1
handoff_id: c91e
parent_handoff_ids: [3d3a]
lineage: deterministic
chain: [standalone-bd7d]
repo: hermes-agent
workspace: memory-guard-forward-port
branch: main
head_sha: 7f571c287e6e4978914d66de0f722a697ca0a898
created_at: 2026-09-21T10:40:00-0400
writer: claude-code
---

# Handoff — memory capacity guard forward-ported; memory_tool*.py now match upstream v2026.9.14

## The Goal

Follow-up #1 from handoff 3d3a: stop `tools/memory_tool.py` conflicting on every upstream merge by
making it (and `tools/memory_tool_store.py`) upstream's own files and re-implementing the fork's
memory capacity guard as an isolated fork-only layer. **Done and live.** Write-up with the hunk
classification and hook list: `docs/MEMORY_GUARD_FORWARD_PORT_2026-09-21.md`.

## Where We Are

Live `~/.hermes/hermes-agent` `main` fast-forwarded from `76017181f1` (rollback point) to the
branch `chore/memory-guard-forward-port`; gateway restarted 10:26 EDT through the operator's
launchd job (PID 65474 -> 50755); platforms connected, herdr-lifecycle-reporter loads.

- `tools/memory_tool_store.py`: byte-identical to v2026.9.14, no hooks.
- `tools/memory_tool.py`: 403 lines (upstream 397); 2 `# FORK(memory-capacity-guard):` hooks
  (rebind `MemoryStore` to `GuardedMemoryStore` + re-exports; `entries_for_read` in
  `_validate_single_op`).
- `tools/memory_capacity_guard.py` (new, 807 lines): `GuardedMemoryStore(MemoryStore)`; wraps upstream's
  `_mutate` funnel (journal when upstream validation says over-limit and the store is in journal mode;
  op context via a ContextVar), plus overlay, fallback JSONL journal, `apply_with_capacity`,
  consolidation helpers, thresholds.
- `tests/tools/test_memory_capacity_guard_layer.py` (new): fails if a merge drops a hook.
- `~/.hermes` commit `2e0ddbc`: `check-gateway-fork-invariants.sh` now checks the module + both hooks.
- Playbook §3/§4/§5 updated: "memory_tool*.py take upstream verbatim except the tagged hooks".

## What We Tried (do not repeat)

1. Overriding each of `add/replace/apply_batch` inline was rejected: upstream's overflow check lives in
   closures inside those methods. Wrapping the `_mutate` callback needs no edits inside them.
2. `_usage_pct` clashes with upstream (fork: `(target)` -> int; upstream: `(target, current)` -> str).
   Solved with an optional second argument, not by renaming (fork tests call `_usage_pct("memory")`).
3. A locale-sorted test file list puts `test_memory_tool_import_fallback.py` before
   `test_memory_tool.py`; upstream's fallback test poisons the `tools.memory_tool` package attribute
   and 3 `TestMemoryFileLockPermissions` tests then fail — reproduced on a pristine v2026.9.14 tree.
   Use `LC_ALL=C sort` / default collection order.
4. The live `.venv` lacks pytest-xdist/pytest-timeout; the `upstream-merge-2026-09` worktree's `.venv` has
   them. From a scratch script use `PYTHONPATH=$PWD`, else the editable install imports another worktree.

## Key Decisions (autonomous; operator not available)

- Behaviour preserved exactly: the scripted scenario (80% reject, 98% add/replace/batch journaled,
  overlay reads, shrinking replace, malformed batch, projection drain, warn note) produced identical
  JSON on pre-change `main` and on the new layer (ids normalised).
- One user-visible change: the `memory` tool schema "WHEN:" text is upstream's current wording (the
  fork copy carried the older text through the compaction drift).
- The guard module is import-time coupled to upstream private helpers (`_apply_batch_op`,
  `_find_unique_match`, `_mutate`, `_edit`, `_read_raw_checked`, `_detect_external_drift`); a rename
  upstream breaks tests in `test_memory_capacity_guard.py` first.

## Evidence & Data

- Before (pre-change main) vs after, 34 memory/guard/write-approval/consolidation files:
  996 passed / 996 passed (+4 new layer tests = 1000 on the rebased tree, C-locale order);
  24 neighbouring files (background review, file safety, cron memory contract, claw, profiles, ...):
  695 passed, 8 skipped after. Guard/write-approval group: capacity_guard 13, projection 17,
  pending_queue 28, pending_migration 21, fault_path 14, write_approval 15, journal_consumer 4,
  memory_tool 57, schema 2, import_fallback 1, threat_patterns 28 = 200 passed before and after.
- Live: `hermes gateway status` supervised by launchd job com.stayturgid.hermes-gateway;
  `shutdown_watchdog` count 4 before and after; both invariant scripts silent; Memory Size Watchdog
  script rc 0 silent (it does not import memory_tool); `hermes -z` -> SMOKE-OK; scratch-HERMES_HOME
  round trip via the live install works (GuardedMemoryStore).

## Where We're Going

1. Watch the gateway log for a day; the guard is exercised only when memory reaches 85%.
2. Cleanup: `git worktree remove ~/.hermes/hermes-agent-worktrees/memory-guard-forward-port` after the soak.
3. Next merge: take upstream's `memory_tool*.py`, re-apply the 2 tagged hooks, run the guard tests.
