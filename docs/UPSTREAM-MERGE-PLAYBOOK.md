# Upstream merge playbook (fork djbclark/hermes-agent ← NousResearch/hermes-agent)

Distilled from the 2026-08-22 (v2026.8.19), 2026-09-21 (v2026.9.14) and 2026-10-18 (v0.22.2)
merges. Copy-pasteable; every command was run as written unless marked UNVERIFIED. Read the latest
`docs/handoffs/HANDOFF_*upstream-merge*` and `docs/UPSTREAM_MERGE_CONFLICT_MAP_*.md` first — they
list what the last merge decided.

The v2026.9.24 merge (`5c551e0d21`, 2026-09-30) left no record of its own; the v0.22.2 conflict map
reconstructs what it decided. Every merge writes both a conflict map and a handoff (§7).

**v0.22.2 changed promotion fundamentally (§6):** upstream's new `pm/` package owns the runtime
environment (Python 3.14 store interpreter, venv under `$HERMES_HOME/installs/`), so the old
`uv pip install -e '.[all]' --python venv/bin/python` step no longer works. §6 is the rehearsed and
run recipe (2026-10-18); §7 lists the cleanups that follow it.

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
# Gate (c) on CI instead of this Mac (preferred since 2026-10-18): free GitHub runners, branch `ci/base` on
# github.com/djbclark/hermes-agent (docs/github-ci-test-ref.md there). From any fork checkout:
#   git fetch origin ci/base && git checkout origin/ci/base -- scripts/github-test-ref
#   scripts/github-test-ref <merged-ref> <pristine-ref>     # 10 shards, ~9 min; pushes only ci/run/**
# (proven 2026-10-19 on fork main: 8m49s, 49726 passed / 20 failed; 17 of the 20 fail on main itself, 3 are CI
# self-tests that need the workflow files the overlay removes)
# CI under load flakes: on 2026-10-18 8 of 9 merged-only ids passed serially; re-run merged-only ids
# serially on both trees (`-p no:xdist`) before calling them merge-caused. (The earlier Hyades/Forgejo
# route, scripts/forgejo-test-ref on ha/base, is retired; that repo was deleted.)
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

Rewritten 2026-10-19 from the v0.22.2 promotion: rehearsed on three scratch copies, run for real on
2026-10-18 (third attempt; the first two were rolled back, see Lessons). Since v0.22.2 the live
checkout runs from a pm-managed environment (`$HERMES_HOME/installs/<sha16(checkout)>/environments/<uuid>/venv`,
store Python 3.14 from `~/.hermes/tools`), not from the checkout's 3.11 `venv/`; every runtime
dependency is gated on `python_version >= '3.14'`. The first launch also publishes
`<checkout>/.hermes/bin/hermes` and rewrites `~/.local/bin/{hermes,hermes-acp,hermes-agent}` (the
shim both `com.stayturgid.hermes-gateway` and `com.djbclark.hermes-dashboard` exec) unless
`cli.expose_on_path: false`. **State is forward-only:** config 34 → 50 and state.db 30 → 31 migrate
and do not migrate back, so `git reset` is NOT a rollback. The rollback script below is.

Run it from a plain Terminal (or an agent session that is not a Hermes session: the gateway
stops mid-run and would take a Telegram/Discord/ACP session with it).

**The drivers live outside the repo** (UNVERIFIED that they persist; copy them somewhere durable
before relying on them, and ask djbclark which repo they belong in):

| script | does |
|---|---|
| `~/.local/state/hermes-promote/prewarm.sh EXTRAS_FILE` | fills pm's uv cache while the gateway is up (step 3) |
| `~/.local/state/hermes-promote/promote-run.sh TARGET_SHA` | the whole window: stop jobs, promote, go/no-go, start jobs, auto-rollback |
| `~/.local/state/bigteam/adhoc-2026-10-18/hm-promo-rehearsal/bin-tools/hermes-promote.sh BK TARGET SRC REF` | backups + fast-forward + `pm install` + first launch + completion tail (generic: arguments only) |
| `~/.local/state/bigteam/adhoc-2026-10-18/hm-promo-rehearsal/bin-tools/hermes-rollback.sh BK [SHA]` | the rollback (generic) |

`promote-run.sh` hard-codes v0.22.2 specifics: the live rollback point
(`09750eb58f9103b4914a6355446905b9cf4cac6d`, checked in its preflight), the pasture path
`~/.cow/pastures/hermes-agent/upstream-merge-2026-10`, branch `chore/upstream-merge-2026-10` and
the backup name `v0.22.2-<date>-<hhmm>`. Copy it and change those constants for the next merge.

**Lessons (2026-10-18).** Each is enforced by the driver, but know why:

a. **Pre-warm pm's uv cache `~/.hermes/cache/uv`** (NOT `~/.cache/uv`) with the gateway still up.
   Attempt 2 (19:21) stopped the gateway, then `pm install`'s `uv sync` crawled at ~15 KB/s, hit pm's
   1800 s limit (`uv sync timed out after 1800s`) and was rolled back after a ~31 min outage.
b. **`launchctl bootout` returns before teardown ends.** Wait for the unload AND for the pid to
   exit before starting or touching anything. Attempt 1 (19:14) saw the gateway "still loaded" 3 s
   after bootout, rolled back, and its own `bootstrap` then failed (`Input/output error`), leaving the
   old gateway down (`OLD GATEWAY NOT UP: needs djbclark`) until it was started by hand. The driver's `out()` waits up to 90 s (and checks
   twice, since a job caught mid-spawn can read as gone), and `boot()` retries 6 times.
c. **Run the driver through `~/ops/site-private/bin/bg`** (its load gate delays the start, before the
   gateway is touched, while the box is overloaded), in the background, never bare.
d. **Back up before touching anything** (state is forward-only). `hermes-promote.sh` takes the
   backups in its step b; do not skip it by running the later steps by hand.
e. **Gate (c) runs on free GitHub runners** (§5), not on this Mac and not on Forgejo/Hyades.
f. **The desktop app is rebuilt after the soak, with the gateway running** (last subsection). On
   2026-10-18 the first launch built it inside the window anyway (see the timing table).

### Before the window (gateway up)

```bash
# 1. gates a-e green on the pasture (§5). Gate (c) on CI:
git fetch origin ci/base && git checkout origin/ci/base -- scripts/github-test-ref
scripts/github-test-ref <merged-ref> <pristine-ref>     # 10 shards, ~9 min; details in §5

# 2. preflight (read-only)
uptime                                          # 1-min load < ~20
U=gui/$(id -u); C=~/.hermes/hermes-agent; P=~/.cow/pastures/hermes-agent/upstream-merge-2026-10
TARGET=$(git -C "$P" rev-parse HEAD)            # the committed merge head; must descend from the live HEAD
git -C $C status --porcelain                    # must print nothing: promote-run.sh refuses a dirty live checkout
git -C $C rev-parse HEAD                        # = the rollback point hard-coded in promote-run.sh
pgrep -fl 'hermes( |$)|hermes-acp'              # the three jobs are expected; ask any interactive hermes/ACP session to finish
df -h ~ | tail -1                               # 2026-10-18: 49 GiB free; backups are APFS clones, but state.db is a 1.6 GB real copy
GATEWAY_FORK_DIR=$P SKIP_LAUNCHCTL=1 bash ~/.hermes/scripts/check-gateway-fork-invariants.sh   # must be silent, rc 0 (the go/no-go runs it too, and any failure rolls back)
```

`<merged-ref>` is the pasture branch and `<pristine-ref>` the upstream tag.

```bash
# 3. pre-warm pm's uv cache. EXTRAS_FILE = one line of space-separated extras, the same list
#    hermes-promote.sh computes in its "extras" step from the live venv/ (2026-10-18: the 22 below).
L=~/.local/state/hermes-promote
echo 'all acp anthropic audio-io bedrock computer-use discord doc-extract edge-tts exa firecrawl google stt-whisper telegram trace-upload vertex vision wake-porcupine wake-sherpa web wecom youtube' > $L/extras.txt
$L/prewarm.sh $L/extras.txt > $L/prewarm.log 2>&1; echo "rc=$?" > $L/prewarm.rc    # background; 16 min on 2026-10-18
```

`prewarm.sh` runs `pm install` for the **pasture** (its own install key) with those extras, retrying
(up to 24 times, 30 s apart): pm's `uv sync` gives up after 1800 s but finished wheels stay in
`~/.hermes/cache/uv`. It ends `PREWARM-OK try N` and `rc=0`; `promote-run.sh` refuses to run unless
`prewarm.rc` is exactly `rc=0`. Re-run it if the pasture's `pyproject.toml`/`uv.lock` change after
the pre-warm (inferred, not tested). The list above is the 2026-10-18 one; `prewarm.log` does not
record how that run passed it, so recompute it if `venv/` gained packages (UNVERIFIED).

### The window

```bash
# 4. move the previous attempt's log/rc aside (a stale promote-run.rc fools a waiter), then run it IN THE BACKGROUND
~/ops/site-private/bin/bg $L/promote-run.sh "$TARGET" > $L/promote-run.log 2>&1; echo "rc=$?" > $L/promote-run.rc
```

Wait on `promote-run.rc`, not on a pipe or a process pattern; read the log for `PROMOTE-OK in N s`,
`ROLLED-BACK` or `PROMOTE-FAIL`. Exit codes: 0 promoted; 2 refused in preflight (nothing was
touched); 3 rolled back, old gateway restarted (`OLD GATEWAY NOT UP: needs djbclark` in the log means
start it by hand, see Rollback). What `promote-run.sh` does, in order:

- **preflight**: live tree clean, live HEAD = rollback point, pasture HEAD = `$TARGET`, `prewarm.rc` = `rc=0`.
- **stop jobs** with `out()`: gateway, then dashboard (writers of state.db first), then memory-journal.
- **`hermes-promote.sh`** (`set -euo pipefail`; any failure rolls back), phases:
   - (a) preflight (tracked tree clean, `$BK` absent, store Python present, `git fetch` of the pasture branch, `$TARGET` is a fast-forward);
   - (b) **backups into `$BK` = `~/.local/state/hermes-promote/v0.22.2-<date>-<hhmm>`**: `cp -c -R` APFS clone of the whole checkout to `checkout.pre/` (the only copy of the old `venv/`, `.venv/`, `web_dist`, `node_modules`, desktop build), `sqlite3 state.db ".backup"`, `config.yaml`, `bin/{hermes,hermes-acp,hermes-agent}`, `HEAD`, `skills.bundled_manifest`, md5s;
   - (c) `git merge --ff-only $TARGET`;
   - (d) extras the live `venv/` carries (restricted to `venv/`, not the stale `.venv/`) → `extras.txt`;
   - (e) `pm install --extra …` with the store Python (`env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV $PY -m pm.cli install`);
   - (f) first launch through the old shim (`~/.local/bin/hermes config check`): adopts `install-stamp.json`, publishes `.hermes/bin/{hermes,hermes-acp}`, rewrites the 3 launchers, migrates config 34 → 50;
   - (g) completion tail (`source_completion.py --finish-update`): Node deps, TUI + web builds, model-catalog seed, bundled-skills sync, final stamp;
   - (h) removes the lefthook `pre-commit` hook v0.22.2's `prepare` installs into the shared `.git` (it would change commit behaviour for every fork worktree and needs `ruff`/`npx`), unless the backup clone had one;
   - (i) opens `SessionDB()` once with the new environment's Python: state.db 30 → 31 under watch instead of inside the gateway's first start;
   - (j) `hermes --version`, `hermes config check` (written to a file then grepped: piping into `grep -m1` raised BrokenPipe under `pipefail` in the rehearsal).
- **go/no-go**: `--version` has `v0.22.2` and `Python: 3.14`; `Config version: 50`; `hermes pm status` rc 0; the shim names `$C/.hermes/bin/hermes`; no lefthook hook; `GATEWAY_FORK_DIR=$C SKIP_LAUNCHCTL=1` fork-invariants script passes. Any NO-GO → rollback.
- **start gateway** (`boot()`), wait up to 240 s for `Gateway running with` in the log past the pre-start byte offset (else rollback), then `hermes gateway status`.
- **dashboard, bridge, memory-journal**: dashboard `boot()`, `launchctl kickstart -k` the MCP bridge, then install the repo's `cron/com.djbclark.hermes-memory-journal.plist` over the LaunchAgent (the original is saved as `$BK/memory-journal.plist.orig`; the repo copy runs `.hermes/bin/hermes --run-module cron.scripts.memory_journal_consumer`) and `boot()` it; prints the dashboard HTTP code and job states.
- **push**: `git push origin main` (not fatal if it fails; retry by hand, never a bare `git push`, §0). The pasture branch push is NOT in the driver: `git -C "$P" push origin chore/upstream-merge-2026-10`.

Timings of the real run (attempt 3, 20:19:21 → `PROMOTE-OK in 511 s`; the gateway was down from
20:19:2x to 20:27:26, about 8 min; the rehearsal numbers are in brackets):

| step | real | |
|---|---|---|
| stop jobs | 6 s | |
| backups | 89 s | [45 s] |
| fast-forward | 6 s | [4 s] |
| `pm install` | 20 s | [123 s warm cache, 353 s cold cache under load] |
| first launch | **280 s** | [24 s]: it also finished the "interrupted source update": TUI, web **and the packaged desktop app** (`hermes_cli/source_build.py` includes `apps/desktop` whenever a packaged desktop app exists; the rehearsal sandbox had none) |
| completion tail | 53 s | [93–128 s] |
| state.db 30 → 31 + checks + go/no-go | 8 s | [6 s for the migration] |
| gateway to "Gateway running with 2 platform(s)" | 24 s | |
| dashboard, bridge, memory-journal, push | 28 s | |

No way to defer the first launch's desktop build was tried. If the 4-5 min matters, test one on a
scratch copy first.

### After the driver says PROMOTE-OK

```bash
# 5. verify by eye (the driver already did the checks; these are the ones it cannot judge)
hermes --version            # "Hermes Agent v0.22.2+…", "Python: 3.14.7"
hermes gateway status       # "Supervised by launchd job com.stayturgid.hermes-gateway"
tail -60 ~/.hermes/logs/gateway.log     # Discord/Telegram connected, "Gateway running with 2 platform(s)", no plugin ImportError
grep -n shutdown_watchdog ~/.hermes/logs/gateway.log | tail -3    # quiet
curl -sI http://100.113.53.87:9119/ | head -1    # 2026-10-18: 302 (a redirect), not the 200 the rehearsal saw
launchctl print $U/com.djbclark.hermes-memory-journal | rg 'last exit|state'    # last exit code = 0
```

Seen in the 2026-10-18 gateway log and not judged: `Signal: cannot reach signal-cli at
http://127.0.0.1:18765` (ERROR) and `sms: TWILIO_PHONE_NUMBER not set` (warning). The gateway still
reached "Gateway running with 2 platform(s)"; whether the Signal error predates the promotion is
UNVERIFIED. The skill curator's LLM call with model `big-pickle` (`stayturgid_hermes_model`) failed
with HTTP 400 in the scratch rehearsal; live it is the same, and older: `skills/.curator_state` still
records `last_run_at: 2026-10-13`, `llm: Model 'big-pickle' isn't available on custom … invalid model
format. Expected format: modelType/model`. The live `model:` block is now `provider: xai-oauth` /
`default: grok-4.7` (2026-10-18), so the curator is passing a model that no longer matches the
provider; its next real run is the test of whether it picks the config default up.

```bash
# 6. push the pasture branch (the driver pushes only main)
git -C "$P" push origin chore/upstream-merge-2026-10
```

Then soak for a day (§7). The gateway and dashboard plists are unchanged: they exec
`~/.local/bin/hermes`, which now execs `$C/.hermes/bin/hermes`.

### Rollback (verified twice, plus the two live rollbacks of 2026-10-18)

The driver rolls back by itself when anything before "Gateway running" fails. By hand, from a plain
shell, with `BK` = the backup directory (its path is printed at the end of `promote-run.log`, and
`ls -d ~/.local/state/hermes-promote/v0.22.2-*`):

```bash
U=gui/$(id -u); BK=~/.local/state/hermes-promote/v0.22.2-<date>-<hhmm>
# bootout returns early: wait for the unload AND the pid to exit (same logic as out() in promote-run.sh)
for j in com.stayturgid.hermes-gateway com.djbclark.hermes-dashboard com.djbclark.hermes-memory-journal; do
  pid=$(launchctl print $U/$j 2>/dev/null | awk '$1=="pid"{print $3; exit}')
  launchctl bootout $U/$j 2>/dev/null
  while launchctl print $U/$j >/dev/null 2>&1 || { [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; }; do sleep 1; done
done
bash ~/.local/state/bigteam/adhoc-2026-10-18/hm-promo-rehearsal/bin-tools/hermes-rollback.sh "$BK"
#   RESTORE_DATA=1 bash …/hermes-rollback.sh "$BK"   # only if state.db/config are damaged: loses post-promotion history
hermes --version            # "Hermes Agent v0.21.5 (2026.9.24) · upstream 09750eb5", "Python: 3.11.15"
cp -p $BK/memory-journal.plist.orig ~/Library/LaunchAgents/com.djbclark.hermes-memory-journal.plist   # only if the driver had already swapped it
launchctl bootstrap $U ~/Library/LaunchAgents/com.stayturgid.hermes-gateway.plist
launchctl bootstrap $U ~/Library/LaunchAgents/com.djbclark.hermes-dashboard.plist
launchctl kickstart -k $U/com.djbclark.hermes-mcp-bridge     # what the driver's own rollback does last
```

(The wait loop is `out()` simplified and was not run as written; the `launchctl` calls and
`hermes-rollback.sh` are the verified ones.) `hermes-rollback.sh BK [SHA]` does, in order:

1. `git reset --keep <HEAD from $BK/HEAD>` in place: refs, branches and worktree metadata made since the promotion are kept (swapping the whole checkout directory back would drop commits that worktree agents made meanwhile);
2. restores the gitignored runtime artifacts from `checkout.pre/` by APFS clone (`venv`, `.venv`, `node_modules`, `web/node_modules`, `ui-tui/node_modules`, `hermes_cli/web_dist`, `ui-tui/dist`, `apps/desktop/{dist,release}`), removing any the promotion created;
3. removes `install-stamp.json`, `.hermes/bin/{hermes,hermes-acp}` and the lefthook hook;
4. restores the 3 `~/.local/bin` launchers (temp file, then `mv -f`);
5. **keeps** state.db (schema 31) and config.yaml (v50) by default: v0.21.5 opens, writes and reads a v31 state.db and accepts a v50 config (34 → 50 changed only `_config_version`), so nothing written since the promotion is lost. `RESTORE_DATA=1` puts the backups back instead. `installs/` is left in place (inert for the old code).

Evidence: restore-data run 97 s (schema 30, `config.yaml` md5 = live's); keep-data run 115 s (v0.21.5 /
3.11.15, launchers byte-identical to the backups, `hermes-acp` runs, 998 sessions, the session
written by the promoted copy survived); old code on the v31 DB creates sessions and appends
messages; old code on the v50 config prints `Config version: 50 ✓`. The two live rollbacks
(attempts 1 and 2) both ended on v0.21.5 / 3.11.15.

Not undone by the rollback (harmless): `installs/<key>` (~0.7 GB; `rm -rf` it after giving up on
the target), the 2 bundled skills synced to the new content (`git -C ~/.hermes checkout --
skills/media/youtube-content skills/software-development/hermes-agent-skill-authoring` if wanted),
and `skills/.curator_state`.

### After the soak: the desktop app

Rebuild with the gateway running, never in the downtime window. The command is UNVERIFIED as a
desktop-only run: `hermes_cli/source_completion.py` takes `--desktop` (read in source), so
`(cd $C && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV $PY -I -B -u hermes_cli/source_completion.py --source $C --finish-update --desktop)`
with `PY` = the store Python (`ls -d ~/.hermes/tools/python-3.14.*/bin/python3 | tail -1`)
should do it. **It re-installs the lefthook `pre-commit` hook** that step (h) removed (measured
2026-10-19 11:04: the rebuild wrote `.git/hooks/pre-commit` and `.git/info/lefthook.checksum`, and
the next commit in the live checkout ran lefthook). Remove both again afterwards — the hook is
shared by every fork worktree and needs `ruff`/`npx`:
`rm -f $C/.git/hooks/pre-commit $C/.git/info/lefthook.checksum`.
Do not swap the running desktop app out from under the operator; leave that to them
(`~/.hermes/skills/hermes/hermes-install-lifecycle/SKILL.md`: the installed app is packaged, so
editing `apps/desktop/src` changes nothing until it is rebuilt, and the running app is theirs to swap).

The pre-v0.22.2 procedure (`uv pip install -e '.[all]' --python venv/bin/python`, then
`hermes gateway restart`) was removed on 2026-10-19: it cannot install v0.22.2+ into the 3.11
`venv/`, `check-hindsight-hook-invariants.sh` is deleted, and a rollback to an older tag is
`hermes-rollback.sh` (it restores `venv/` from the backup clone). It is in git history at
`a6b187d8a6`.

## 7. Afterwards

1. **Handoff + conflict map:** `docs/handoffs/HANDOFF_standalone-bd7d_upstream-merge-<phase>_<date>_<4hex>.md`
   (same frontmatter schema; parent = previous handoff id) and
   `docs/UPSTREAM_MERGE_CONFLICT_MAP_<date>_<tag>.md`, both committed.
2. **Soak a day.** Watch `~/.hermes/logs/gateway.log` (tolerant MCP list: a dropped-tool warning, not
   a dead server), then do the cleanups below.
3. **Keep the old `venv/` until every old-venv consumer is migrated** — which, as of 2026-10-19, is
   not possible for most of them. They keep working only because `venv/` stays and the new code still
   imports on 3.11. Find them with
   `rg -l 'hermes-agent/(\.)?venv' ~/.hermes/scripts ~/Library/LaunchAgents`.
   **`--run-module` takes a dotted module name only** (the launcher does
   `runpy.run_module`): no script path, no `-c`, no stdin. So only a consumer that runs a module
   inside the checkout, or the `hermes` CLI itself, can move. On 2026-10-19 (`~/.hermes` commit
   `f92bb42`) that was two: `scripts/memory-journal-consumer.sh` (now
   `.hermes/bin/hermes --run-module cron.scripts.memory_journal_consumer`, the same form as
   `cron/com.djbclark.hermes-memory-journal.plist`) and `scripts/ai-frontier-weekly-research.sh`
   (a plain CLI call: `.hermes/bin/hermes -z …` / `send`). Still on `venv/bin/python`, each because it
   runs a file by path or a stdin heredoc: `scripts/{memory-metrics-report,memory-size-watchdog,
   tcc-audit-watchdog,gmail-index-sync,provider-health-watchdog}.sh`, plus the `xai-oauth-bridge`
   LaunchAgent (`~/ops/site-djbclark/roles/litellm/defaults/main.yml`
   `litellm_xai_bridge_python`; its script imports `hermes_cli.auth`, and the pm env's venv path
   contains a per-sync uuid, so there is no stable interpreter path to point it at — change the
   Ansible role, never the live plist). The Hermes cron job `8486ce778b40` (Provider Health Watchdog)
   is disabled; only its `last_error` names the path. Each of those needs either a module shipped
   inside the checkout (the `cron.scripts.*` route) or a stdlib-only interpreter
   (`tcc-audit-watchdog.sh` and `gmail-index-sync.sh` import stdlib only and already fall back to
   `/usr/bin/python3`). Delete `venv/` and `.venv/` only once none is left.
4. **`hermes pm gc`** reclaims what nothing references: unreferenced store entries, dependency and
   runtime generations, and the `installs/<key>` state of **deleted** checkouts (`pm/cli.py` `cmd_gc`;
   read, not run). `~/.hermes/installs/` held 4 borrowed environments from Oct 8 worktree test runs
   (2.8 GiB: `38818021…`, `6246458f…` = `~/src/hermes-worktrees/fix-config-bigint`, `68ac85a5…`,
   `c7de12bd…`) plus the merge pasture's own key; they only go once their worktree/pasture is removed
   (item 7). Never `rm -rf` the live checkout's own `installs/<key>`.
   **Measured 2026-10-19: `hermes pm gc` freed nothing** (`gc: removed 0, kept 9; removed 0
   dependency generations, 0 PM runtime generations, 0 install state dir(s) of deleted checkouts`),
   although three of those five borrowed keys (2.1 GiB) hash to directories that no longer exist —
   `collect_orphan_install_states` evidently judges orphanhood by something else. It has no
   `--dry-run`: to see what it would touch, hash the candidate checkouts yourself
   (`sha256(realpath)[:16]` = the `installs/` key, `pm/environments.py` `install_key`) before
   running it. Removing a borrowed `installs/<key>` by hand is the operator's call.
5. **Re-check the fork-invariant script** `~/.hermes/scripts/check-gateway-fork-invariants.sh`:
   update symbol/file checks if a fork helper moved (2026-09: update restart moved to
   `update_cmd_fleet.py`; check 2b now greps `gateway_launchd.py`, fixed in `~/.hermes` commit
   `0aaffdd`); commit only that file in `~/.hermes`. Run it with `GATEWAY_FORK_DIR=$C SKIP_LAUNCHCTL=1`;
   on 2026-10-19 it passed against the live checkout. Check 6 now asserts the whole shim line
   (`exec <checkout>/.hermes/bin/hermes "$@"`, override `HERMES_BIN_TARGET`) instead of the
   substring "hermes-agent", and a new check 7 greps three fork-only tags an upstream merge could
   drop silently (`FORK(launchctl-guard)`, exactly one `def _spawn_typescript(`,
   `FORK(opencode-zen-keyless)`) — `~/.hermes` commit `01de18c`. The
   old hindsight invariants script and its pytest line no longer apply.
6. **Optional: `hermes sessions optimize-storage`** (v0.22.2 advises it: ~0.9 GB of the 1.6 GB
   state.db). Run it after the soak, in the foreground, **with the gateway/dashboard/cron writers
   stopped** (it refuses while the store is held unless `--force`; wait out the `bootout` as in the
   rollback block), with free disk ≈ the DB size (`--no-vacuum` skips the VACUUM and the headroom).
   It prints a progress bar and is safe to Ctrl-C and re-run. Back up `state.db` first.
   **Also stop `com.djbclark.hermes-mcp-bridge`** (2026-10-20): it spawns a `hermes mcp serve` child
   that holds `state.db`/`-wal`/`-shm`, so with only gateway/dashboard/journal down the command still
   refuses, naming that child's pid. `com.djbclark.hermes-mcp-auth` does not hold the store and can
   stay up. Restart the bridge afterwards with
   `launchctl kickstart -k gui/$(id -u)/com.djbclark.hermes-mcp-bridge`; any Claude session using the
   `hermes` MCP tools loses them until it reconnects. Never reach for `--force`: the refusal is the
   feature, and it names the holder.
   **Pass `--yes`.** It asks `Proceed? [y/N]` for the disk-space confirmation, and with no TTY
   `input()` raises EOFError, which it reads as "no": it prints `Cancelled.` and exits **0**. A
   backgrounded or piped invocation therefore looks like a success that reclaimed nothing (2026-10-20:
   it cost a whole attempt). `--yes` / `-y` is the supported way past that prompt; do not pipe
   `printf 'y\n' |` into it. Judge the result by the before/after size, never by the exit status —
   upstream NousResearch/hermes-agent#123322 tracks the wider family of `hermes sessions` failure
   paths that return 0, and the no-TTY cancel is a sibling of those.
7. **Remove the merge pastures** after the soak: `$W remove upstream-merge-$YM` and the pristine one
   (handles the immutable-flag files; `--force` if it is dirty), then `git branch -d` the merged local
   branches. Do NOT bulk-delete other local branches: several `fix/*`/`feat/*` branches hold commits
   that are not in `main` (`git rev-list --count main..<branch>`); review before pruning.
8. **Backups: gone.** djbclark accepted the v0.22.2 promotion on 2026-10-20 and
   `~/.local/state/hermes-promote/v0.22.2-20261018-{1921,2019}` were removed (6.9 GB *each*, 13.8 GB
   together — `du` counts APFS-shared blocks, judge with `df`). There is no longer a restorable 3.11
   `venv/` or a pre-promotion `state.db`. The small provenance files from both (both `config.yaml`s,
   the go/no-go logs, `md5.before`, `HEAD`, the bundled-skill manifests, `memory-journal.plist.orig`)
   are kept in `~/.local/state/hermes-promote/v0.22.2-20261018-metadata.tar.gz` (10 KB). For the next
   promotion the rule stands: remove the backups, and the failed-attempt directory, only once the
   promotion is accepted.
9. `~/.hermes/skills/hermes/hermes-install-lifecycle/SKILL.md` points here.

## 8. Disk notes (measured 2026-09-21)

- Pastures cost ~nothing; stale pastures and worktrees are what cost disk. Removing the nine old
  fork-feature pastures freed ~3.6 GB, the two worktrees ~0.8 GB, aborted-fetch `tmp_pack_*`
  debris in `.git/objects/pack` another ~0.55 GB (`git count-objects -v` warns "garbage found").
- `cow stats` "On disk" is `du`-style (counts shared blocks), so judge savings with `df`, before and
  after, not with that column. `cow gc --merged --dry-run` lists pastures whose branch merged.
- Check every pasture's HEAD is reachable from a live-repo branch before removing it
  (`git branch -a --contains <sha>`); only un-shallowed history makes that check trustworthy.
