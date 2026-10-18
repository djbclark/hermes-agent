# Upstream merge playbook (fork djbclark/hermes-agent ← NousResearch/hermes-agent)

Distilled from the 2026-08-22 (v2026.8.19), 2026-09-21 (v2026.9.14) and 2026-10-18 (v0.22.2)
merges. Copy-pasteable; every command was run as written unless marked UNVERIFIED. Read the latest
`docs/handoffs/HANDOFF_*upstream-merge*` and `docs/UPSTREAM_MERGE_CONFLICT_MAP_*.md` first — they
list what the last merge decided.

The v2026.9.24 merge (`5c551e0d21`, 2026-09-30) left no record of its own; the v0.22.2 conflict map
reconstructs what it decided. Every merge writes both a conflict map and a handoff (§7).

**v0.22.2 changed promotion fundamentally (§6):** upstream's new `pm/` package owns the runtime
environment (Python 3.14 store interpreter, venv under `$HERMES_HOME/installs/`), so the old
`uv pip install -e '.[all]' --python venv/bin/python` step no longer works.

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
  `package-lock.json`. Never bump beyond the merged lockfile (`tool.uv exclude-newer`, "3 days" at
  v0.22.2). Since v0.22.2 `pm/` owns the runtime environment: never raw `pip`/`uv pip` into it
  (`pm/AGENTS.md`); a pasture's own `.venv` for tests is fine.
- Tests can reach real `launchctl`: the fork's foreign-launchd helpers boot out / kickstart the
  operator's `com.stayturgid.hermes-gateway`. Upstream's test live-system guard only blocks
  `systemctl` and process killers; the fork adds `FORK(launchctl-guard)` in
  `tests/_fixtures/live_system_guard.py`. Re-apply it if a merge drops it, BEFORE running any test.
- The box is often memory-bound (16 GB, swapping) rather than CPU-bound; `uptime` alone
  understates the risk. `~/ops/site-private/bin/bg` adds its own load gate and machine-wide test slots.

## 1. Pick the target and prepare the repo

```bash
cd ~/.hermes/hermes-agent
git fetch upstream --tags
git tag -l --sort=-creatordate | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -3   # newest RELEASE TAG is the target, not upstream/main
T=v0.22.2                                       # example
git merge-base --is-ancestor $T upstream/main && echo tag-on-main
git rev-parse --short HEAD                      # ROLLBACK POINT — write it down
git rev-parse --is-shallow-repository           # must print false; if true see §2
git merge-base main $T || echo EMPTY            # must print the previous upstream side; if EMPTY see §2
```

Tag scheme (changed 2026-10): upstream moved from CalVer `v2026.M.D` (last: `v2026.9.24`) to
semver `vX.Y.Z` (`v0.22.1` 2026-10-14, `v0.22.2` 2026-10-15; `v0.22.2` contains `v2026.9.24`).
It also tags a nightly `vX.Y.Z+canary.<timestamp>` plus `rc.N-vX.Y.Z` and `abandoned-rc.N-vX.Y.Z`,
so an unfiltered `--sort=-creatordate` lists only canaries. Never target a canary or rc; the filter
above keeps plain `vX.Y.Z` by date. Don't sort by version (`-v:refname`): it ranks the old
`v2026.9.24` above `v0.22.2`.

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
YM=$(date +%Y-%m); W=~/src/djbclark-ade/bin/cow-pasture
P=$(cow create upstream-merge-$YM --source ~/.hermes/hermes-agent --branch chore/upstream-merge-$YM --no-symlink --print-path | tail -1)
rm -rf "$P/.venv" "$P/venv"  # copied 3.11 venvs are useless (rebuilt in §5) and make scrub crawl
$W scrub "$P"; cd "$P"      # removes gitignored secret-like files; add `$W trust "$P"` only for Claude Code sessions
git rev-parse --is-shallow-repository                # false
git diff --name-only $(git merge-base main $T) HEAD > /tmp/fork_delta.txt        # fork feature files
git -c merge.renameLimit=20000 merge --no-commit --no-ff $T; git diff --name-only --diff-filter=U   # conflict list
```

Scrub cost (2026-10-18): `cow-pasture scrub` forks `basename` once per ignored file; with the
copied `.venv`/`venv`/`node_modules` (~160k files) it ran >12 min, raised the load, and deleted
`credentials.py`/`cacert.pem` inside the copied venvs (harmless only because they are rebuilt).
Deleting the venvs first, as above, leaves only `node_modules`.

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
   v0.22.2 specifics: most conflicts were upstream's `Dict`/`List` → `dict`/`list` typing sweep
   colliding with fork signature additions (take upstream's typing, keep the fork params);
   `hermes_cli/gateway.py::_cmd_start` now runs inside `with start_scope:` (fork hook goes inside);
   `agent/lsp` gained workspace trust (`UNTRUSTED_SAFE_SERVERS`, 3-arg `_client_key`, `_live_key`);
   update code split into `update_cmd_check/commit/validation/...`, `update_completion`,
   `venv_sync`, `source_completion`; `hermes_cli/update_cmd_deps.py` deleted. After resolving,
   look for a fork function that upstream ALSO defines now (Python keeps the later definition
   silently): v0.22.2 had two `_spawn_typescript`s. Run
   `ruff check --isolated --select F821,F811,F823,F822` over the fork-touched `.py` files and diff
   the findings against the same files at `$T`.
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
   Signature drift seen so far: v2026.9.24 `_apply_batch_op` → `(error, previous)`, `_edit` gained
   `matched_entry`, `_success_response(**extra)`; v0.22.2 `_apply_batch_op` → 4-tuple
   `(error, previous, failure_class, patched_entry)` (the guard now reads `[0]`) and a new
   `patch` action (`MemoryStore.patch`/`_patch`, span edit) that the guard does NOT journal yet.
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
tools/file_tools.py `_find_truncation_placeholder(content)` and
`_find_truncation_placeholder(_extract_v4a_added_content(patch))` (tools/truncation_markers.py);
compressor marker: switched to upstream's `agent/compression_marker.py` at v2026.9.24 (the fork's
`_format_compression_marker` is gone on purpose); `/clinepass` + `/aiuse` handlers (cli_commands_mixin,
cli_info_mixin, slash_commands_model, slash_commands_status, commands.py, commands_platforms.py
`"aiuse", "clinepass"`, tui_gateway/methods_slash.py, desktop `AIUSE_SLASH_TIMEOUT_MS` +
`timeoutMs: surface?.kind === 'exec'` in use-prompt-actions/slash.ts); agent/reasoning_params.py
`"cline.bot"`; shutdown_forensics `ctx["supervisor"]`.
Added 2026-10-18 (fork features since v2026.9.14): Telegram `_clarify_button_label` (choice text on
clarify buttons); hermes_cli/web_server_config.py `_stringify_unsafe_ints`; acp_adapter/server.py
named custom provider recovery (96e2e32104); tools/mcp_tool.py Streamable HTTP probe when
`ClientSession` is pre-bound (044a39f35a; tests in test_startup_latency_regressions.py); keyless
`opencode-free` (`FORK(opencode-zen-keyless)`: auth.py registry row + aliases, providers.py label,
hermes_cli/opencode_zen_keyless.py, plugins/model-providers/opencode-free); agent/lsp
`_PYRIGHT_BINS`, `_server("ruff"`/`"marksman"`/`"taplo"`, `_discover_tsserver_js`,
`RUST_ANALYZER_WAIT_TIMEOUT`, manager `_matching_servers` (and ruff/marksman/taplo in
`UNTRUSTED_SAFE_SERVERS`); tests/_fixtures/live_system_guard.py `FORK(launchctl-guard)`.
Runnable version: `bash docs/fork-feature-checklist.sh <tree>` (prints MISSING lines, `missing=N`);
extend it when a fork feature lands.
`~/.hermes/scripts/check-gateway-fork-invariants.sh` greps most of these.

## 5. Verification gates (in order)

```bash
uv sync --frozen --all-extras --no-extra matrix --group dev --group test && uv pip install --python .venv/bin/python pytest-xdist pytest-timeout
# v0.22.2: `default-groups = []`, so without `--group dev` there is no pytest-asyncio and every async
# test fails. `.python-version` = 3.14 (uv builds a 3.14 .venv). First sync after a big jump downloads
# ~200 wheels (29 min on 2026-10-18); later ones clone from the uv cache.
# (`uv sync` removes the two plugins, reinstall them every time.)
.venv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"   # must equal the pyproject pin (2.0.0)
export HERMES_HOME=$(mktemp -d)                                           # never the live ~/.hermes
P="$HOME/ops/site-private/bin/bg nice -n 15 .venv/bin/python -m pytest -q -p no:cacheprovider --timeout 180 --timeout-method=thread"
# A collection error inside an xdist worker shows up as an INTERNALERROR "assert not crashitem";
# re-run that file with `-p no:xdist` to see the real error (2026-10-18: retired `macos_only` mark).
# Upstream's own runner is scripts/run_tests.sh (one pytest subprocess per file); the chunked
# pytest below + pristine comparison remains the fork's method.
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
Q=$(cow create upstream-pristine-$T --source ~/.hermes/hermes-agent --branch pristine-$T --no-symlink --print-path | tail -1)
rm -rf "$Q/.venv" "$Q/venv"; git -C "$Q" checkout -q --detach $T; ln -s "$P/.venv" "$Q/.venv"   # same venv; never a worktree of the live .git
# ids with spaces/braces: tr '\n' '\0' < ids.txt | xargs -0 nice -n 15 .venv/bin/python -m pytest ...
# Only ids that fail on merged but pass on pristine are merge-caused. Expect ~90 environmental
# failures identical on both (web dashboard, update autostash/self-lock, voice, install.sh, /tmp path asserts).
# v0.22.2: ~350 environmental at -n 4 on a loaded Mac (also real-`gh` release scripts, termux, connector RPC
# timing, xdist sys.modules pollution such as a leaked `telegram` MagicMock). Fork-only parametrizations
# (`[opencode-free]`) do not exist on pristine and pytest aborts the whole run on one unknown id: intersect
# the id list with `pytest --collect-only -q <files>` on the pristine tree first.
# Alternative for gate (c), used 2026-10-18 when the Mac was starved: Hyades CI on forgejo.mit.edu
# (`scripts/forgejo-test-ref <merged-ref> <pristine-ref>` on the Forgejo-only `ha/base` branch; upstream's
# per-file runner, merged and pristine as two parallel jobs). Run 16 hit the 75-min job deadline at ~94 %
# (alphabetically up to tests/tools/test_x*), so run the tail of tests/tools locally.
# gate (d): NEVER against the live ~/.hermes. A v0.22.2 launch migrates config.yaml (34 -> 50, writes a .bak),
# creates installs/, and bumps state.db (30 -> 31) forward-only. Use a throwaway home:
H=$(mktemp -d); cp ~/.hermes/config.yaml "$H/"; ln -s ~/.hermes/.env "$H/.env"
HERMES_HOME=$H HERMES_DISABLE_LAZY_INSTALLS=1 timeout 240 .venv/bin/python hermes -z "Reply with exactly: SMOKE-OK"; rm -rf "$H"
# gate (e): bash docs/fork-feature-checklist.sh "$P"; also slack_native_slashes() must be <= 50 (50 at v0.22.2).
```

Upstream tests that enumerate providers or stores catch fork additions; expect to adapt them (with a
`FORK(...)` note) after each merge, as at v0.22.2: e2e catalog (`tests/e2e/core/providers/_catalog_helpers.py`
`KEYLESS_PROVIDERS` + `allow_paid_opencode_zen` in `write_home`), `tests/fixtures/provider_wire_snapshot.json`
(record a new fork provider with `HERMES_UPDATE_WIRE_SNAPSHOT=1` on just its cell),
`tests/fixtures/resolution_allowlist.json` (fork `shutil.which` sites), provider catalog / desktop parity
exemptions, the upstream "opencode-free was removed" test, and anything reading `pending/memory/*.json`
(the fork keeps MEMORY approvals in `memory_pending.db`).

Known quirk (upstream, reproduced on a pristine tag): if `test_memory_tool_import_fallback.py`
runs before `test_memory_tool.py` in one process, 3 `TestMemoryFileLockPermissions` tests fail.
Default collection order is fine; a locale-sorted file list triggers it (`LC_ALL=C sort` avoids it).
The live dev venv `~/.hermes/hermes-agent/.venv` was rebuilt with the command above on
2026-09-21 (it had lagged at mcp 1.28.1 and lacked xdist/timeout), so it can also run the gates.

## 6. Promotion and rollback

**OBSOLETE FROM v0.22.2 — do not run the block below as written.** It installs into the checkout's
3.11 `venv/`; v0.22.2 gates every runtime dependency on `python_version >= '3.14'` and runs from a
pm-managed environment (`$HERMES_HOME/installs/<sha16(checkout)>/environments/<uuid>/venv`, store
Python 3.14 from `~/.hermes/tools`). `update_cmd_deps._refresh_active_lazy_features` is now a stub
that calls `stop_for_relaunch()`. Also new: the first launch publishes `<checkout>/.hermes/bin/hermes`
and rewrites `~/.local/bin/hermes` (the shim both `com.stayturgid.hermes-gateway` and
`com.djbclark.hermes-dashboard` exec) unless `cli.expose_on_path: false`; state DB schema 30 → 31
and config version → 50 migrate forward-only. `git reset` alone is therefore NOT a rollback any more
(back up `~/.hermes/state.db`, `config.yaml` and `~/.local/bin/hermes` first). The v0.22.2 handoff
carries an UNVERIFIED candidate sequence (`python -m pm.cli install` with the store Python, extras
from `pm.extras.legacy_selection`, then a first launch from a shell); the lead must validate it on
a scratch copy before the first promotion and then rewrite this section.

Pre-v0.22.2 procedure, kept for reference and rollback to an older tag:

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
  2026-10-18: its check 2b still greps `hermes_cli/gateway.py` for the install guard, which has lived
  in `hermes_cli/gateway_launchd.py` since the v2026.9.24 split, so it already fails on live `main`.
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
