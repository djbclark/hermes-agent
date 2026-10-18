---
schema_version: 1
handoff_id: 3d3a
parent_handoff_ids: [934a]
lineage: deterministic
chain: [standalone-bd7d]
repo: hermes-agent
workspace: upstream-merge
branch: main
head_sha: de826d9fff0778f63e6bbcea2a2508d900adaac9
created_at: 2026-09-21T09:55:00-0400
writer: claude-code
---

# Handoff — Phase 3 SHIPPED: upstream v2026.9.14 merged, every fork feature reviewed, live gateway promoted

## The Goal

Update the fork to the newest upstream release tag while keeping every fork change, with a new
operator rule applied this time: for EVERY fork feature, check whether upstream now implements it
and switch to upstream's version where it does (tested per feature). Then promote, verify, and
document so the next merge is cheap. **Done and live.** Playbook:
`docs/UPSTREAM-MERGE-PLAYBOOK.md`. Per-file and per-feature decisions with test evidence:
`docs/UPSTREAM_MERGE_CONFLICT_MAP_2026-09-21_v2026.9.14.md`.

## Where We Are

Live `~/.hermes/hermes-agent` `main` = `de826d9fff`, pushed to `origin/main` (branch
`chore/upstream-merge-2026-09` pushed too). Rollback point was `11e6f40d9a`. Gateway restarted
09:46 EDT through the operator's launchd job (PID 9385 → 65474); Telegram/Signal/Discord connected,
"Gateway running with 3 platform(s)". `hermes --version` → `Hermes Agent v0.21.3 (2026.9.14)`.
Both watchdog scripts silent. hindsight-retention-pilot plugin tests 42 passed.

Commit stack (worktree branch, fast-forwarded into main):

| Commit | What |
|---|---|
| `e630b28892` | Merge upstream v2026.9.14 (base fcbd1076a9), 38 conflicted paths resolved |
| `0df6696664` | `_launchd_user_home` helper restored; memory pending queue chmods its own dir |
| `2b8a139192` | slash-dispatch test adapted for /aiuse + /clinepass; google-workspace setup.py back to upstream pins; /clinepass + /aiuse documented (doc parity test) |
| `de826d9fff` | playbook + conflict map |

### Target and base

Target = newest upstream release tag `v2026.9.14` (345cd2b057, on upstream/main; tip was
3917c7dd6a a week newer — untagged churn, rejected as in phase 2). Base = `fcbd1076a9` via a
real 3-way merge — but only after fixing `.git/shallow`: `hermes update`'s
`git fetch --depth 1` had marked the fork's own tips (`11e6f40d9a`, `ae230f072e`,
`c4fc319704`) as shallow boundaries, so `git log main` showed one commit and merge-base was
empty. Removed those three lines (parents present locally), merged, restored the file
byte-identical afterwards (`.git/shallow.bak-graft-20260921` confirmed by `diff`; safe to delete).
No replace refs needed.

### Per-feature review (the new rule) — summary

Switched to upstream (3): /status recency route (`get_recent_session_model_route` is the same
query as our `get_latest_session_model_usage`), empty `tool_calls` coercion (now in
`agent/transports/chat_completions.py`), dropping `--replace` from launchd ProgramArguments
(`_timestamped_stderr_gateway_command`, #79048; our pinning test kept). Dependency pins: upstream's.
Kept ours (everything else — 16 features), re-implemented inside upstream's restructured
modules; full table with test evidence in the conflict map.

### Test verdict

~52,300 tests in 9 chunks. Raw 268 failures → 97 consistent when re-run isolated on the merged
tree → 92 identical on a pristine v2026.9.14 worktree with the same venv. Merge-caused: 6 total,
all fixed (pending-queue dir permissions; google-workspace pins ×4; slash-command doc parity ×1)
plus the `_launchd_user_home` test patch target. Gate (a) `test_gateway_service.py`: 131 passed /
5 skipped. Feature suites: 293 (clinepass/aiuse/validation/picker/status/clipboard/zen), 172
(memory guard + write approval), 273 (runtime provider/model switch/opencode), 350 (MoA/compressor/
file guard/forensics/sanitization). `hermes -z` → `SMOKE-OK`.

## What We Tried (do not repeat)

1. **Ran chunks at `-n 8` concurrently with other work → the LIVE gateway was starved.** Its
   liveness watchdog (`gateway.shutdown_watchdog`, "missed 3 consecutive liveness probes", exit
   75, launchd respawn) fired at 06:19:54 and 06:50:55 (gateway.log; the coordinator observed load
   ~866 with 26 pytest + 12 git processes). Hard rule now in the playbook: one chunk at a time,
   `nice -n 15`, `-n 4` max, never merged + pristine suites concurrently, check `uptime` first.
2. **Session hit the usage limit (HTTP 429) at ~06:50** mid-chunk; resumed 08:52. The
   `hermes_cli-1` chunk had finished (1h02 under contention) so its result was kept and its
   failures re-run isolated like every other chunk's.
3. Node ids with spaces/braces break `$(cat ids)`; use `tr '\n' '\0' | xargs -0`.
4. A log line captured as an ERROR id (`plugins.memory.mem0:...`) aborts collection — filter ids
   to `^tests/`.
5. `hermes gateway status`'s manual-branch hint list and `_launchd_user_home` were both
   inlined/removed upstream; the fork's tests patch `_launchd_user_home`, so it is a helper again.

## Key Decisions (autonomous; operator not available)

- **`tools/memory_tool.py` = ours wholesale** (upstream split MemoryStore into
  `memory_tool_store.py`; forward-porting the 600-line capacity guard into the new `_mutate/_edit`
  design was judged riskier than reverse-porting upstream's five fixes: secure 0600/O_NOFOLLOW
  lock files, oversize-on-load warning, empty-store batch refusal #103419, background-review
  delete gate #105921, PEP 562 plugin shim). Upstream's `memory_tool_store.py` is present but
  unused. Follow-up candidate: forward-port the guard so this file stops conflicting.
- Fork-only logic that upstream keeps restructuring went into its own module where possible
  (`tools/mcp_tolerant_list.py`), and `/clinepass`/`/aiuse` rely on upstream's
  `_handle_<name>_command` naming fallback instead of table entries.
- Fork docs stay under `docs/` although upstream deleted that directory.
- `apps/desktop/package.json` electron went back to upstream's 40.10.2 so it matches
  `package-lock.json` (never bump beyond the merged lockfile).
- `tools/memory_pending_queue.py` now chmods its own directory (upstream's state-DB preflight no
  longer leaves HERMES_HOME at 0700).

## Evidence & Data

- Live: PID 65474 under `com.stayturgid.hermes-gateway`; agent.log shows
  `tools.mcp_tolerant_list` installed and `dropped malformed tool 'cards__checkout_profile'`
  (the behavioural check for the tolerant MCP list) with the server still registered.
- Pre-existing, unrelated: `Failed to load plugin 'herdr-lifecycle-reporter': No module named
  'herdr_lifecycle'` (49× before the restart today, 108× yesterday, since 2026-09-11);
  `hindsight-retention-pilot`, `litellm-provider`, `opencode-go-display` show "not enabled" because
  `plugins.enabled` in config.yaml does not list them.
- Slack: `slack_native_slashes()` == 50 with aiuse/clinepass via `/hermes`.
- Runtime venv: `uv pip install -e '.[all]' --python venv/bin/python` (openai 2.24.0 per upstream
  pins — a downgrade from the fork's 2.53.0 bump), lazy refresh: 7 refreshed / 13 current.
- `~/.hermes` commit `86f7064`: watchdog now checks `update_cmd_fleet.py` for the post-update
  foreign restart.

## Where We're Going

1. Operator: tap `/clinepass` and `/status` on Telegram (full-width picker + upstream's recency
   route); watch the gateway log for a day.
2. Cleanup: `git worktree remove ~/.hermes/hermes-agent-worktrees/upstream-merge-2026-09`
   (keep until the soak is over — its `.venv` has pytest); pristine worktree already removed;
   delete `.git/shallow.bak-graft-20260921` (confirmed identical).
3. Follow-ups: forward-port the memory capacity guard into `memory_tool_store.py`; add a unit test
   for `tools/mcp_tolerant_list.py`; consider teaching `hermes update`'s shallow check not to
   re-shallow fork tips (or un-shallow the clone once: `git fetch --unshallow upstream`).
4. Next merge: follow `docs/UPSTREAM-MERGE-PLAYBOOK.md` §1–§7 verbatim.
