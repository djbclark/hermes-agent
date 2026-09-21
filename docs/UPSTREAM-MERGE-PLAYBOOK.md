# Upstream merge playbook (fork djbclark/hermes-agent ← NousResearch/hermes-agent)

Distilled from the 2026-08-22 (v2026.8.19) and 2026-09-21 (v2026.9.14) merges. Copy-pasteable;
every command was run as written. Read the latest `docs/handoffs/HANDOFF_*upstream-merge*` and
`docs/UPSTREAM_MERGE_CONFLICT_MAP_*.md` first — they list what the last merge decided.

## 0. Hard rules

- **The live gateway shares this box.** Its liveness watchdog (`gateway.shutdown_watchdog`)
  kills it after 3 missed probes when the machine is starved — it happened twice on 2026-09-21
  during `-n 8` test runs (load average ~800). Run test chunks **one at a time**, under
  `nice -n 15`, with `-n 4` at most, never merged-tree and pristine-tree suites concurrently,
  and check `uptime` first (wait if the 1-minute load is above ~20).
- Never merge in `~/.hermes/hermes-agent` (the checkout the gateway runs from). Work in a cow
  pasture (§3; a git worktree also works); promote by fast-forward only.
- Never `hermes update` (syncs `origin` only, not fork-aware) and never `hermes gateway install`
  (a second KeepAlive job would fight `com.stayturgid.hermes-gateway`).
- Never bare `git push` there: `main` tracks `upstream/main`. Always `git push origin main`.
- Dependency pins: take upstream's `pyproject.toml`/`uv.lock`/`tools/lazy_deps.py`/
  `package-lock.json`. Never bump beyond the merged lockfile (`tool.uv exclude-newer = "14 days"`).

## 1. Pick the target and prepare the repo

```bash
cd ~/.hermes/hermes-agent
git fetch upstream --tags
git tag --sort=-creatordate | head -3          # newest RELEASE TAG is the target, not upstream/main
T=v2026.9.14                                    # example
git merge-base --is-ancestor $T upstream/main && echo tag-on-main
git rev-parse --short HEAD                      # ROLLBACK POINT — write it down
git rev-parse --is-shallow-repository           # must print false; if true see §2
git merge-base main $T || echo EMPTY            # must print the previous upstream side; if EMPTY see §2
```

## 2. Keeping the clone un-shallow (fixed 2026-09-21)

The clone used to be shallow: `hermes update` ran `git fetch --depth 1` and marked each fetched
tip (including the fork's own commits) as a shallow boundary, which emptied `git merge-base`
against upstream. On 2026-09-21 it was un-shallowed, so `git merge-base main <tag>` works
directly and `hermes update` (never run it here, §0) would now do plain fetches. The fork
watchdog (`check-gateway-fork-invariants.sh`) alerts if `.git/shallow` ever reappears. Repair:

```bash
cd ~/.hermes/hermes-agent
git fetch --unshallow origin; git fetch --unshallow upstream      # seconds each, ~50 MB
git rev-parse --is-shallow-repository                              # true + one line left in .git/shallow?
# A leftover boundary whose parents exist locally is stale. Move the file aside, verify, then delete:
cp .git/shallow /tmp/shallow.bak && mv .git/shallow .git/shallow.disabled
git rev-list --objects --missing=print --all | grep -c '^?'        # must print 0
git fsck --connectivity-only --no-dangling                         # must be clean
rm .git/shallow.disabled            # only if both passed; otherwise: mv .git/shallow.disabled .git/shallow
```
(`git --shallow-file=` is NOT a git option; do not try it.) Do the repair while no other git
process or workspace is using this `.git`. Legacy note: before 2026-09-21 the fix was deleting
only the fork-tip lines from `.git/shallow` for the merge and restoring the file afterwards
(phase-2/3 handoffs describe it); `git replace --graft` never worked here because shallow
boundaries override replace refs.

## 3. Pasture, merge, conflict policy

A cow pasture (APFS copy-on-write clone) is the workspace: its own `.git` (nothing shared with the
live repo), a full copy of the tree, and near-zero disk (measured 2026-09-21: creating and
removing one changed free space by ~0.06 GB). `cow` symlinks large dirs (`.venv`, `venv`,
`node_modules`) back to the source by default; use `--no-symlink` for a merge so `uv sync` in the
pasture cannot touch the live venvs. The wrapper does not pass `--no-symlink`, so create with bare
`cow`, then scrub secrets with the wrapper:

```bash
YM=$(date +%Y-%m); W=~/orca/projects/djbclark-ade/bin/cow-pasture
P=$(cow create upstream-merge-$YM --source ~/.hermes/hermes-agent --branch chore/upstream-merge-$YM --no-symlink --print-path | tail -1)
$W scrub "$P"; cd "$P"      # removes gitignored secret-like files; add `$W trust "$P"` only for Claude Code sessions
git rev-parse --is-shallow-repository                # false
git diff --name-only $(git merge-base main $T) HEAD > /tmp/fork_delta.txt        # fork feature files
git merge --no-commit --no-ff $T; git diff --name-only --diff-filter=U               # conflict list
```

Per conflicted file, decide in this order:
1. **Does upstream now implement the fork feature?** Grep the tag for the *behaviour* (not our
   symbol names) and `git log --oneline <base>..$T -- <file>`. If yes: take upstream, drop ours,
   adapt/delete our tests, run that feature's tests. Record it in the decision table.
2. Otherwise **keep both**: take `git checkout --theirs -- <file>` when upstream restructured the
   file, then re-apply the fork's delta (`git diff <base> HEAD -- <file>`) into the new home
   (upstream splits big modules aggressively: `gateway/run.py` → `run_shutdown.py`,
   `hermes_cli/update_cmd.py` → `update_cmd_fleet.py`, `hermes_cli/models.py` →
   `models_validate.py`, `tools/mcp_tool.py` → `mcp_tool_loop.py`, etc.). Anchor edits with
   asserted string replacements; fork-only logic goes into its own module when possible
   (e.g. `tools/mcp_tolerant_list.py`) so the next merge does not conflict.
3. `tools/memory_tool.py` and `tools/memory_tool_store.py` take **upstream's version verbatim
   except the tagged hooks**: `git checkout --theirs` (or `$T -- <file>`), then re-apply the
   `# FORK(memory-capacity-guard):` hooks (`grep -rn "FORK(memory-capacity-guard)" tools/`; there are
   two, both in `tools/memory_tool.py`, listed with the rationale in
   `docs/MEMORY_GUARD_FORWARD_PORT_2026-09-21.md`). The guard itself lives in the fork-only
   `tools/memory_capacity_guard.py` (`GuardedMemoryStore(MemoryStore)`), which never conflicts;
   after a merge just check it still works against upstream's `MemoryStore` internals
   (`_mutate`, `_edit`, `_apply_batch_op`, `_find_unique_match`, `_success_response`,
   `_render_block`, `_read_raw_checked`, `_detect_external_drift`) via
   `tests/tools/test_memory_capacity_guard*.py`, `test_memory_projection.py`,
   `test_memory_pending_queue.py`, `test_memory_fault_path.py`, `test_write_approval.py`.
   Do NOT hand-port upstream memory fixes into the fork any more; they arrive natively.
4. Fork docs stay in `docs/` (upstream deleted it; git auto-moves them under `website/docs/`).
5. Dep files: theirs. `apps/desktop/package.json` must match `package-lock.json`.

Write `docs/UPSTREAM_MERGE_CONFLICT_MAP_<date>_<tag>.md` as you go (file → resolution, plus the
feature → kept-ours / switched-to-upstream table with test evidence). Commit the merge before
running tests so fixes land as separate commits.

## 4. Fork-feature checklist (all must be PRESENT after the merge)

hermes_cli/gateway.py: `_parse_launchd_label_for_pid`, `_find_foreign_launchd_gateway`,
`_restart_foreign_launchd_gateway`, `_find_foreign_launchd_gateway_plist`,
`_stop_foreign_launchd_gateway`, `_start_foreign_launchd_gateway`, `_launchd_user_home`, routing
strings `_restart_foreign_launchd_gateway(*foreign)`, `_stop_foreign_launchd_gateway(*foreign)`,
`_start_foreign_launchd_gateway(*foreign_plist)`, `Supervised by launchd job`, install guard
`operator-managed gateway LaunchAgent already exists`, `pids.add(foreign[2])`;
hermes_cli/update_cmd_fleet.py `_restart_foreign_launchd_gateway(f_label, f_domain, f_pid)`;
gateway/run_shutdown.py `should_restart_after_signal()`; tools/mcp_tolerant_list.py +
`install_tolerant_list_tools()` in tools/mcp_tool_loop.py; Telegram `_register_clipboard_copy`,
`"cc:"` callback, `interactive_resume = False`; `full_width` in the three adapters and
gateway/slash_commands_model.py; tools/memory_capacity_guard.py `apply_with_capacity`, `MEMORY_QUEUE_PCT`, `GuardedMemoryStore` + tools/memory_tool.py `FORK(memory-capacity-guard)` hooks (2);
tools/write_approval.py `_pq.enqueue(`; agent/moa_loop.py `_record_reference_cooldown`;
hermes_cli/models_validate.py `_validate_discovery_disabled`; runtime_provider_custom.py
`"x-api-key"`; runtime_provider.py `allow_paid_opencode_zen`; model_cost_guard.py `is_free_model`;
tools/file_tools.py `_find_truncation_placeholder(content)`; agent/context_compressor.py
`_format_compression_marker`; `/clinepass` + `/aiuse` handlers (cli_commands_mixin,
cli_info_mixin, slash_commands_model, slash_commands_status, commands.py, commands_platforms.py
`"aiuse", "clinepass"`); agent/reasoning_params.py `"cline.bot"`; shutdown_forensics
`ctx["supervisor"]`. `~/.hermes/scripts/check-gateway-fork-invariants.sh` greps most of these.

## 5. Verification gates (in order)

```bash
uv sync --frozen --all-extras --no-extra matrix && uv pip install --python .venv/bin/python pytest-xdist pytest-timeout
# (`uv sync` removes the two plugins, reinstall them every time; it clones from the uv cache, ~free.)
.venv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"   # must equal the pyproject pin (2.0.0)
export HERMES_HOME=$(mktemp -d)                                           # never the live ~/.hermes
P="nice -n 15 .venv/bin/python -m pytest -q -p no:cacheprovider --timeout 180 --timeout-method=thread"
$P tests/hermes_cli/test_gateway_service.py                                          # gate (a)
$P tests/hermes_cli/test_clinepass_command.py tests/gateway/test_clinepass_command.py \
   tests/hermes_cli/test_aiuse_command.py tests/hermes_cli/test_model_validation.py \
   tests/gateway/test_choice_picker.py tests/gateway/test_telegram_clipboard_copy_button.py \
   tests/gateway/test_status_command.py tests/hermes_cli/test_commands.py \
   tests/hermes_cli/test_opencode_zen_free_keyless.py tests/hermes_cli/test_opencode_zen_free_policy.py \
   tests/tools/test_memory_tool.py tests/tools/test_memory_capacity_guard.py tests/tools/test_memory_capacity_guard_layer.py tests/tools/test_memory_pending_queue.py \
   tests/tools/test_write_approval.py tests/agent/test_moa_reference_cooldown.py tests/tools/test_file_tools.py \
   tests/gateway/test_runner_startup_failures.py tests/gateway/test_shutdown_forensics.py \
   tests/tools/test_mcp_tolerant_list.py   # gate (b); its lenient-listing tests skip unless mcp is 2.x
# gate (c): full suite, one chunk at a time (split tests/gateway, tests/hermes_cli, tests/agent+hermes_state,
# tests/tools into halves, then the rest; ~2-20 min each at -n 4). Collect FAILED/ERROR ids, re-run them
# isolated on the merged tree, then the SAME ids on a pristine worktree of the tag with the same venv:
git -C ~/.hermes/hermes-agent worktree add --detach ~/.hermes/hermes-agent-worktrees/upstream-pristine-$T $T
# ids with spaces/braces: tr '\n' '\0' < ids.txt | xargs -0 nice -n 15 .venv/bin/python -m pytest ...
# Only ids that fail on merged but pass on pristine are merge-caused. Expect ~90 environmental
# failures identical on both (web dashboard, update autostash/self-lock, voice, install.sh, /tmp path asserts).
timeout 240 .venv/bin/python hermes -z "Reply with exactly: SMOKE-OK"                # gate (d), live config
# gate (e): the §4 grep checklist; also slack_native_slashes() must be <= 50.
```

Known quirk (upstream, reproduced on a pristine tag): if `test_memory_tool_import_fallback.py`
runs before `test_memory_tool.py` in one process, 3 `TestMemoryFileLockPermissions` tests fail.
Default collection order is fine; a locale-sorted file list triggers it (`LC_ALL=C sort` avoids it).
The live dev venv `~/.hermes/hermes-agent/.venv` was rebuilt with the command above on
2026-09-21 (it had lagged at mcp 1.28.1 and lacked xdist/timeout), so it can also run the gates.

## 6. Promotion and rollback

```bash
cd ~/.hermes/hermes-agent && git fetch "$P" chore/upstream-merge-$YM && git merge --ff-only FETCH_HEAD   # live tree must be clean
uv pip install -e '.[all]' --python venv/bin/python                     # runtime venv (what `hermes update` does)
venv/bin/python -c "from hermes_cli.update_cmd_deps import _refresh_active_lazy_features as r; print(r())"   # refresh active lazy backends
export PATH="$HOME/.local/bin:$PATH"; hermes gateway restart              # from a plain shell, never inside a Hermes session
hermes gateway status            # "Supervised by launchd job com.stayturgid.hermes-gateway"
launchctl list | grep hermes-gateway; hermes --version
tail -60 ~/.hermes/logs/gateway.log     # Telegram/Signal/Discord connected, "Gateway running with N platform(s)", no plugin load errors
grep -n shutdown_watchdog ~/.hermes/logs/gateway.log | tail -3
bash ~/.hermes/scripts/check-gateway-fork-invariants.sh; bash ~/.hermes/scripts/check-hindsight-hook-invariants.sh   # both silent
HERMES_HOME=$(mktemp -d) venv/bin/python -m pytest ~/.hermes/plugins/hindsight-retention-pilot/tests -q
git push origin main && git -C "$P" push origin chore/upstream-merge-$YM
```
Rollback if any check fails: `git reset --hard <ROLLBACK_SHA>` in the live checkout,
`uv pip install -e '.[all]' --python venv/bin/python`, `hermes gateway restart`, re-check status.

## 7. Afterwards

- Handoff: `docs/handoffs/HANDOFF_standalone-bd7d_upstream-merge-<phase>_<date>_<4hex>.md`
  (same frontmatter schema; parent = previous handoff id).
- `~/.hermes/scripts/check-gateway-fork-invariants.sh`: update symbol/file checks if a fork helper
  moved (2026-09: update restart moved to `update_cmd_fleet.py`); commit only that file in `~/.hermes`.
- `~/.hermes/skills/hermes/hermes-install-lifecycle/SKILL.md` points here.
- After a day's soak: `$W remove upstream-merge-$YM` (handles the immutable-flag files; `--force` if
  it is dirty), then `git branch -d` the merged local branches. Do NOT bulk-delete other local
  branches: several `fix/*`/`feat/*` branches hold commits that are not in `main` (list them with
  `git rev-list --count main..<branch>`); review before pruning.
- Watch the gateway log for a day (tolerant MCP list: a dropped-tool warning, not a dead server).

## 8. Disk notes (measured 2026-09-21)

- Pastures cost ~nothing; stale pastures and worktrees are what cost disk. Removing the nine old
  fork-feature pastures freed ~3.6 GB, the two worktrees ~0.8 GB, aborted-fetch `tmp_pack_*`
  debris in `.git/objects/pack` another ~0.55 GB (`git count-objects -v` warns "garbage found").
- `cow stats` "On disk" is `du`-style (counts shared blocks), so judge savings with `df`, before and
  after, not with that column. `cow gc --merged --dry-run` lists pastures whose branch merged.
- Check every pasture's HEAD is reachable from a live-repo branch before removing it
  (`git branch -a --contains <sha>`); only un-shallowed history makes that check trustworthy.
