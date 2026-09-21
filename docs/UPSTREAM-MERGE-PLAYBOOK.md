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
- Never merge in `~/.hermes/hermes-agent` (the checkout the gateway runs from). Work in a
  worktree; promote by fast-forward only.
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
git merge-base main $T || echo EMPTY            # if EMPTY, see §2
```

## 2. Shallow clone surgery (needed when merge-base is EMPTY)

The repo is a shallow clone. `hermes update`'s check path runs `git fetch --depth 1` and marks
each fetched tip as a shallow boundary, so the fork's own commits end up in `.git/shallow` and
`git log`/`merge-base` stop dead. Remove only boundaries whose parents exist locally:

```bash
cp -p .git/shallow .git/shallow.bak-graft-$(date +%Y%m%d)
while read s; do p=$(git cat-file -p $s | awk '/^parent/{print $2}'); ok=1
  for q in $p; do git cat-file -e $q 2>/dev/null || ok=0; done
  [ -n "$p" ] && [ $ok = 1 ] && echo "$s  (parents present: removable)"; done < .git/shallow
# edit .git/shallow: delete ONLY the fork-tip lines listed above (ae230f072e, c4fc319704, 11e6f40d9a in 2026-09)
git merge-base main $T                          # must print the previous merge's upstream side
```
Restore it byte-identical when done (`cp -p .git/shallow.bak-graft-* .git/shallow` after
verifying with `diff`) and `git replace -d` anything you added. The `.git` dir is shared by all
worktrees. (2026-08 needed a `git replace --graft` of the squashed root; since `22fedc8a36` the
fork has real ancestry and only the shallow lines matter.)

## 3. Worktree, merge, conflict policy

```bash
git worktree add ~/.hermes/hermes-agent-worktrees/upstream-merge-$(date +%Y-%m) -b chore/upstream-merge-$(date +%Y-%m) main
cd ~/.hermes/hermes-agent-worktrees/upstream-merge-*/
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
uv sync --all-extras --no-extra matrix && uv pip install --python .venv/bin/python pytest-xdist pytest-timeout
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
   tests/gateway/test_runner_startup_failures.py tests/gateway/test_shutdown_forensics.py   # gate (b)
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

## 6. Promotion and rollback

```bash
cp -p ~/.hermes/hermes-agent/.git/shallow.bak-graft-* ~/.hermes/hermes-agent/.git/shallow   # restore first
cd ~/.hermes/hermes-agent && git merge --ff-only chore/upstream-merge-<ym>
uv pip install -e '.[all]' --python venv/bin/python                     # runtime venv (what `hermes update` does)
venv/bin/python -c "from hermes_cli.update_cmd_deps import _refresh_active_lazy_features as r; print(r())"   # refresh active lazy backends
export PATH="$HOME/.local/bin:$PATH"; hermes gateway restart              # from a plain shell, never inside a Hermes session
hermes gateway status            # "Supervised by launchd job com.stayturgid.hermes-gateway"
launchctl list | grep hermes-gateway; hermes --version
tail -60 ~/.hermes/logs/gateway.log     # Telegram/Signal/Discord connected, "Gateway running with N platform(s)", no plugin load errors
grep -n shutdown_watchdog ~/.hermes/logs/gateway.log | tail -3
bash ~/.hermes/scripts/check-gateway-fork-invariants.sh; bash ~/.hermes/scripts/check-hindsight-hook-invariants.sh   # both silent
HERMES_HOME=$(mktemp -d) venv/bin/python -m pytest ~/.hermes/plugins/hindsight-retention-pilot/tests -q
git push origin main && git push origin chore/upstream-merge-<ym>
```
Rollback if any check fails: `git reset --hard <ROLLBACK_SHA>` in the live checkout,
`uv pip install -e '.[all]' --python venv/bin/python`, `hermes gateway restart`, re-check status.

## 7. Afterwards

- Handoff: `docs/handoffs/HANDOFF_standalone-bd7d_upstream-merge-<phase>_<date>_<4hex>.md`
  (same frontmatter schema; parent = previous handoff id).
- `~/.hermes/scripts/check-gateway-fork-invariants.sh`: update symbol/file checks if a fork helper
  moved (2026-09: update restart moved to `update_cmd_fleet.py`); commit only that file in `~/.hermes`.
- `~/.hermes/skills/hermes/hermes-install-lifecycle/SKILL.md` points here.
- Delete the worktrees (`git worktree remove ...`) and the shallow backup once `diff` confirms it.
- Watch the gateway log for a day (tolerant MCP list: a dropped-tool warning, not a dead server).
