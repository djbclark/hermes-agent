# Upstream merge conflict map — v0.22.2 (2026-10-18)

Merge of NousResearch/hermes-agent tag `v0.22.2` (`d923a5eb48`, 2026-10-15, the first semver release
after CalVer `v2026.9.24`) into the fork. Pre-merge live `main` = `09750eb58f` (rollback point). Merge
base `f97608f178` = `v2026.9.24`, the upstream side of the previous merge `5c551e0d21`. Upstream range:
12,940 commits, 10,362 files. Fork delta vs base: 118 files (42 fork-only, 76 shared). Git reported
21 conflicted paths (all content conflicts; no modify/delete). Worked in cow pasture
`~/.cow/pastures/hermes-agent/upstream-merge-2026-10`, branch `chore/upstream-merge-2026-10`.

Merge commit `a9a4ed2a73`; follow-ups `780f9cf9c6` (guard 4-tuple), `260adc20c4`/`35e95bf8b8` (test launchctl guard),
`123b0847cf`, `08dda69363`, `5751eed1ff`, `e1ef4484e5`, `d37fcf8faf` — see *Test verdict*.
Playbook: `docs/UPSTREAM-MERGE-PLAYBOOK.md`.

## What the v2026.9.24 merge (`5c551e0d21`) decided (it left no record; reconstructed here)

Read from its commit message and diff:
- `tools/memory_capacity_guard.py` adapted to upstream signature changes (`_apply_batch_op` returns
  `(error, previous)`, `_edit` takes `matched_entry`, `_success_response` takes `**extra`).
- `agent/context_compressor.py` taken **whole from upstream**: the fork's `_format_compression_marker` was
  dropped in favour of upstream's own `agent/compression_marker.py` (`⟪HERMES-CONTEXT-COMPRESSION: … ⟫`, the
  same marker shape the fork introduced, plus upstream's dispatch-side `_COMPRESSION_MARKER_ARTIFACT_RE`
  guard). The fork's write-path guard (`tools/truncation_markers.py`, used by `tools/file_tools.py`) stays and
  its needle `HERMES-CONTEXT-COMPRESSION` matches upstream's marker. The §4 checklist still listed
  `_format_compression_marker`; it is now replaced by this note (switched to upstream at v2026.9.24).
- The fork carried the keyless `opencode-free` provider (`FORK(opencode-zen-keyless)`, 20 tags) through that
  merge although upstream removed it ("relay 403s FreeTierError"); kept again here.
- Upstream OAuth `iss` fixes came in.

## Per-feature decision table

| Fork feature | Upstream equivalent at v0.22.2? | Decision | Evidence |
|---|---|---|---|
| Operator-managed launchd label (helpers, restart/stop/start/status routing, `_get_service_pids` foreign pid, install guard) | No (still only `ai.hermes.gateway*`) | **kept ours**; start hook moved inside upstream's new `with start_scope:` block in `_cmd_start`; `_restart_all_as_host` returns after reloading the foreign job | gate (a) |
| Post-update restart of the foreign job (`update_cmd_fleet.py`) | No | **kept ours** (auto-merged) | `test_update_restarts_foreign_launchd_job` |
| `should_restart_after_signal()` launchd clean-exit guard | No | **kept ours** (auto-merged) | `tests/gateway/test_runner_startup_failures.py` |
| Tolerant MCP `tools/list` | No | **kept ours** (fork-only module) | `tests/tools/test_mcp_tolerant_list.py` |
| MCP Streamable HTTP detection when `ClientSession` is pre-bound (044a39f35a) | No (upstream still short-circuits on `ClientSession is not None`) | **kept ours**; upstream's direct-attribute assert taken in the shared test | `tests/tools/test_startup_latency_regressions.py` |
| Telegram clipboard-copy button | No | **kept ours** | `tests/gateway/test_telegram_clipboard_copy_button.py` |
| Telegram `interactive_resume = False` | Mechanism yes, default True | **kept ours** | adapter import |
| Telegram clarify choice text on buttons (ae79241f54) | No (still `str(idx + 1)`) | **kept ours**, with upstream's i18n `t("platform.telegram.prompt.other")` label for the Other button | `tests/gateway/test_telegram_clarify_buttons.py` |
| `full_width` picker rows (3 adapters + gateway picker) | No | **kept ours**; signatures re-typed to upstream's `dict[...]` | `tests/gateway/test_choice_picker.py`, `test_telegram_choice_picker_rows.py` |
| Memory capacity guard | No | **kept ours**: `tools/memory_tool.py` = upstream verbatim (incl. its `replace`→`patch` action rename, `FAILURE_CLASS`, `PATCH_DROP_HINT`) + the 2 `FORK(memory-capacity-guard)` hooks | memory suites in gate (b) |
| Write-approval MEMORY journal branches | No | **kept ours** (`_pq_record_to_pending`, `list_pending` MEMORY branch) | `tests/tools/test_write_approval.py` |
| MoA reference-advisor cooldown | No | **kept ours** on top of upstream's new `agent=` kwarg to `_run_reference` | `tests/agent/test_moa_reference_cooldown.py` |
| `discover_models: false` rung | No | **kept ours** (auto-merged) | `tests/hermes_cli/test_model_validation.py` |
| OpenCode Zen `x-api-key` | No | **kept ours** (auto-merged) | zen tests |
| ClinePass no-spend gate (`allow_paid_opencode_zen`) | No | **kept ours**; signature re-typed | zen/runtime tests |
| Keyless `opencode-free` provider + plugin | No — upstream **removed** it and added an "it was removed" hint | **kept ours** (operator's provider); `providers.py` label row merged with upstream's `local`→`custom` rename | provider tests |
| Truncation-placeholder write/patch guard | No (upstream guards at dispatch, not write) | **kept ours**, before upstream's new 4-arg `_rewrite_v4a_patch_paths_for_host` | `tests/tools/test_file_tools.py` |
| Compressor marker | **Yes** — `agent/compression_marker.py` (since v2026.9.24) | **switched to upstream** at v2026.9.24 (recorded now) | `tests/agent/test_context_compressor.py` |
| `/clinepass`, `/aiuse` (CLI, gateway, registry, Slack, TUI, desktop 90 s exec timeout) | No | **kept ours**; desktop `slash.ts` takes upstream's shared-metrics block then the fork's per-command `timeoutMs` ctx | clinepass/aiuse suites |
| `cline.bot` reasoning params | No | **kept ours** | clinepass gateway tests |
| `shutdown_forensics` supervisor labelling | No | **kept ours** | `tests/gateway/test_shutdown_forensics.py` |
| Web config ints > 2**53 served as strings (ad9678e638) | No | **kept ours**; signature re-typed | `tests/hermes_cli/test_web_server.py` |
| ACP named custom providers keep their endpoint (96e2e32104) | No | **kept ours** (auto-merged) | ACP tests |
| LSP: basedpyright preference, ruff/marksman/taplo servers, multi-server dispatch, per-server wait floor, deep-merged init, retry-once spawn, `hermes lsp which` overrides | No | **kept ours** on top of upstream's new **workspace trust** (`UNTRUSTED_SAFE_SERVERS`, 3-arg `_client_key`, `_live_key`, `env_for("node")`). See notes below | `tests/agent/lsp/` |
| Memory journal cron consumer | No | **kept ours** (fork-only) | `tests/cron/test_memory_journal_consumer.py` |
| Dependency pins | upstream's | **theirs** (fork no longer bumps; v2026.9.24 already took upstream's) | `uv sync --frozen` |

### LSP merge notes (judgment calls)

1. Upstream and fork both defined `_spawn_typescript` (upstream: pin `tsserver.path` to Hermes's SDK in an
   untrusted workspace; fork: inject a classic `tsserver.js` because brew's typescript-language-server with
   TS 7 fails `initialize`). In the merged file the later (fork) definition would have silently replaced
   upstream's. Folded into one: untrusted → upstream's SDK pin, falling back to the fork's
   `_discover_tsserver_js` (Hermes lsp tree, the server's own tree, npm global prefix — never the
   workspace); trusted → the fork's TS 7 injection.
2. `_matching_servers` (fork multi-server dispatch) now applies upstream's trust gate: the logging
   `_untrusted_denied` on the normal path, a silent filter on the `include_broken` readers
   (wait budget, merged diagnostics read, mark-broken) so an untrusted server is never marked broken.
   `enabled_for` calls upstream's `_note_operator_roots()` first.
3. **DECIDED (flag for operator):** `ruff`, `marksman`, `taplo` (fork-registered) were added to
   `UNTRUSTED_SAFE_SERVERS`. They are static analysers that never execute project code, the same class as
   upstream's `yaml-language-server`/`bash-language-server`. Without this they would silently stop
   starting in untrusted checkouts. Remove them from the set to get upstream's stricter default.
4. All client-cache lookups use upstream's `_live_key(srv, root)` (under `_state_lock`).

## File → resolution (21 conflicted paths)

- `agent/lsp/manager.py` — union: upstream trust gate + `_live_key`; fork `_matching_servers` loops (notes above).
- `agent/lsp/servers.py` — union: fork `_merge_init` + upstream `env_for("node")` in `_make_spec`; fork
  basedpyright sibling lookup + upstream trusted-only project venv in `_spawn_pyright`; `_server()` takes both
  `untrusted_init` and `wait_timeout`/`binaries`; fork SERVERS head (ruff); typescript entry keeps
  `which=` for `hermes lsp which`; `__all__` union; one `_spawn_typescript`.
- `agent/moa_loop.py` — upstream `agent=` + fork `reference_cooldown_seconds` and cooldown skip.
- `apps/desktop/.../use-prompt-actions/slash.ts` — upstream shared-metrics block, fork ctx with `timeoutMs`.
- `apps/desktop/.../use-prompt-actions/index.test.tsx` — both imports.
- `hermes_cli/auth.py` — fork `_provider_is_keyless` + upstream `dict` return type.
- `hermes_cli/gateway.py` — theirs for `_cmd_start` restructure + fork start hook inside `with start_scope:`;
  fork `_restart_all_as_host` foreign reload (now `return`s before upstream's `if kind is not None`);
  status: fork `if foreign is None` hint guard + upstream's s6 `snapshot.service_running` branch.
- `hermes_cli/gateway_launchd.py` — fork install guard, then upstream's `not (force or force_unit_path)`.
- `hermes_cli/providers.py` — upstream label row (`custom`) + fork `opencode-free`.
- `hermes_cli/runtime_provider.py` — fork kwarg + upstream return type.
- `hermes_cli/web_server_config.py` — fork `_stringify_unsafe_ints` block + upstream signature.
- `plugins/platforms/{discord,matrix}/adapter.py` — upstream typing + fork `full_width`.
- `plugins/platforms/telegram/adapter.py` (7 hunks) — upstream typing + fork clipboard state/params, clarify
  labels with upstream i18n Other label, `full_width`.
- `tests/agent/lsp/test_client_e2e.py` — `_client(..., *, seed=False, **extra_env)`.
- `tests/gateway/test_shutdown_forensics.py` — fork launchd test kept.
- `tests/hermes_cli/test_gateway_service.py` — fork `TestForeignLaunchdGateway*` classes kept; upstream
  replaced `TestDetectVenvDir` with `_seed_pm_environment` (pm), taken.
- `tests/tools/test_startup_latency_regressions.py` — fork HTTP-detection tests + upstream's assert form.
- `tools/file_tools.py` — fork guard + upstream 4-arg path rewrite.
- `tools/memory_tool.py` — upstream verbatim + 2 hooks.
- `tools/write_approval.py` — fork MEMORY branches + upstream typing.

Static check: `ruff --select F821,F811,F823,F822` over the 97 fork-touched Python files reports nothing new
versus the same files at `v0.22.2` except the intentional `MemoryStore` rebind and a local `Path` re-import.

## Upstream changes that matter beyond the conflicts

- **`pm/` owns Hermes environments (new).** The runtime venv moved to
  `$HERMES_HOME/installs/<sha16(checkout)>/environments/<uuid>/venv` on a PM store Python 3.14; every runtime
  dependency is gated `python_version >= '3.14'`; `hermes_cli/update_cmd_deps.py` was deleted
  (`old_updater_deps.py` stubs call `stop_for_relaunch()`). The playbook §6 promotion recipe is obsolete;
  see the playbook's §6 rewrite note and the handoff.
- State DB schema 30 → 31 (auto, forward-only); config `_config_version` default 46 → 50.
- New update modules (`update_cmd_check/commit/validation/pause_gate/...`, `update_finish`, `update_stage`,
  `update_completion`, `venv_sync`, `source_completion`), new gateway modules (`run_inbound_consumer`,
  `run_inbound_media`, `run_notifications_ownership`, `run_plugin_injection`, `run_shutdown_session_end`),
  `hermes_cli/gateway_service_owner.py`, `agent/compression_marker.py`, `tui_gateway/methods_*` splits.
  Deleted: `hermes_cli/update_cmd_deps.py`, `hermes_cli/gateway_service_unit.py`, `gateway/startup_watchdog.py`,
  `hermes_cli/dep_ensure.py`, the Home Assistant platform/tool, `tools/tirith_security.py`.
- Typing modernisation (`Dict`/`List` → `dict`/`list`) across most modules: the most common conflict cause.

## Test verdict

Machine was memory-bound all day (load 20-130, 15/16 GB); every local pytest ran through
`~/ops/site-private/bin/bg nice -n 15`, `-n 4` at most, one suite at a time, `HERMES_HOME=$(mktemp -d)`.
The live gateway (PID 20916) stayed up throughout.

| Gate | Result |
|---|---|
| (a) `test_gateway_service.py` | 128 pass / 3 fail / 5 skip → 3 fixed (`123b0847cf`, `35e95bf8b8`), re-run green (131 pass / 5 skip) |
| (b) 30 fork-feature files | 951 pass / 9 fail / 8 skip. 5 fail identically on pristine (environmental: 4 × `test_runner_startup_failures` — upstream `home_io_guard` on a pre-set `HERMES_HOME`; 1 × `test_status_command` slack onboarding). 4 merge-caused, fixed in `08dda69363` |
| (c) full suite | Split: local chunks (tests/ root + ci/providers/skills/…, tui_gateway+scripts+cron, hermes_state+e2e+pm+plugins, tools ×2, agent ⅓) and Hyades CI run 16 (forgejo run id 589: `snap/merged-08dda69363` vs `snap/pristine-v0.22.2`, upstream's per-file runner; both jobs hit the 75-min job deadline at ~94 %, i.e. everything alphabetically before `tests/tools/test_x*`; the local tools chunks cover the tail). See below |
| (d) `hermes -z "Reply with exactly: SMOKE-OK"` | **SMOKE-OK**, run with a throwaway `HERMES_HOME` (copy of `config.yaml`, `.env` symlinked read-only) and `HERMES_DISABLE_LAZY_INSTALLS=1` — never the live home: a plain v0.22.2 launch auto-migrates `config.yaml` 34 → 50 (writing a `.bak`), creates `installs/`, and would bump `state.db` 30 → 31. `~/.local/bin/hermes` md5 unchanged |
| (e) §4 checklist / Slack | `bash docs/fork-feature-checklist.sh` → `missing=0`; `slack_native_slashes()` = **50** (exactly at the ≤ 50 cap) |

**Gate (c) detail.** Local: 518 failing ids across the 6 chunks. On pristine (same venv, `-n 4`) 514 of
them collect; 350 fail there too (environmental: release/CI scripts calling the real `gh`, install.sh,
termux, icon flavours, voice/tts, connector RPC timing, xdist `sys.modules` pollution such as the
`telegram` MagicMock leak behind 94 `test_telegram_update_admission` failures and the
`test_telegram_polling_progress_ptb` ImportError, both green serially). The 164 that pass on pristine were
re-run on merged: 99 pass (order/load flakes), 65 fail → all traced to fork code. CI run 16 added 18
merged-only ids (13 files; 1 pristine-only file). Every merged-only failure, with its fix:

| Failure | Cause | Fix |
|---|---|---|
| `test_memory_tool::test_failures_name_the_fix` | fork guard journals from 85 % instead of refusing | test lifts `MEMORY_QUEUE_PCT` (`08dda69363`) |
| `test_write_approval::{unpinned_legacy_remove, non_dict_record, staged_hint_headless}` | MEMORY records live in `memory_pending.db`; journal dedupes identical writes; headless hint named the empty `pending/memory/` dir | hint now names the journal DB (**code fix**); tests adapted (`08dda69363`) |
| `test_memory_patch_action::test_patch_requires_old_text` | `entries_for_read` only exists on `GuardedMemoryStore` (latent fork bug, also on live `main`) | `getattr(store, "entries_for_read", store._entries_for)` (**code fix**, `5751eed1ff`) |
| `test_managed_runtime_resolution::test_bare_which…` | upstream's new resolution audit flags fork `/aiuse` and LSP `shutil.which` | two reviewed allowlist rows (`5751eed1ff`) |
| `test_mcp_tool::TestHTTPConfig::test_http_unavailable_raises` | fork HTTP re-probe (044a39f35a) defeats the patch → real request to example.com | test stubs the probe (`5751eed1ff`) |
| `test_auxiliary_client::…local_nim…`, `test_auxiliary_main_first::…copilot…` | fork keyless-header hook set `default_headers=None` on every client | only set when non-None (**code fix**, `e1ef4484e5`) |
| e2e `test_catalog_fallback` (all rows), `test_catalog_matrix_*[opencode-*]`, `test_catalog_listing[opencode-free]` | fork keyless `opencode-free` has no key env; fork free-only guard blocks the fake's unpriced Zen model | `opencode-free` in `KEYLESS_PROVIDERS`; e2e homes set `allow_paid_opencode_zen` (`e1ef4484e5`) |
| e2e `test_store_traversal` (17) | pending ids read from `pending/memory/*.json` | read the journal DB (`e1ef4484e5`) |
| `test_provider_wire_snapshot[opencode-free]`, `::covers_every_provider` | fork provider absent from upstream snapshot | recorded the cell (pure addition) (`d37fcf8faf`) |
| `test_provider_catalog::…credential_env_var`, `test_provider_parity::…desktop` | keyless provider | exempt `opencode-free` (`d37fcf8faf`) |
| `test_runtime_provider_resolution::test_removed_keyless_free_provider…` ×3 | upstream removed `opencode-free`; fork keeps it | test asserts all aliases resolve to `opencode-free` (`d37fcf8faf`) |
| `test_pool_missing_endpoint` ×4 | fork free-only policy | tests pass `allow_paid_opencode_zen=True` (`d37fcf8faf`) |
| `test_subprocess_text_encoding[aiuse_command.py]` | fork `/aiuse` subprocess lacked `encoding=` | `encoding="utf-8", errors="replace"` (**code fix**, `d37fcf8faf`) |
| `test_fresh_source_install[missing-wheel]` (CI only) | `install.sh` subprocess timeout under CI load; skipped locally | environmental (not merge-caused) |
| `test_local_interrupt_cleanup::…foreground_command…` (local `-n 4` only) | passes serially on merged | flake |

All of the above re-run green on merged after the fixes (targeted files: 198, 130, 429, 17, 900, 690
passed). Not re-run: a second full CI pass at the final SHA (optional; see handoff).

## Fork features retired

None. No fork feature has a v0.22.2 upstream equivalent beyond the compressor marker, which was already
switched to upstream at v2026.9.24. Upstream *removed* `opencode-free`; the fork keeps it.

## `~/.hermes/scripts/check-gateway-fork-invariants.sh` — changes needed (NOT edited here)

Run against this pasture (`GATEWAY_FORK_DIR=<pasture> SKIP_LAUNCHCTL=1`): one failure, the same one it
already reports on live `main`.

1. Check 2b greps `hermes_cli/gateway.py` for `operator-managed gateway LaunchAgent already exists`; the
   guard lives in `hermes_cli/gateway_launchd.py`. Point that grep at `gateway_launchd.py` (pre-existing bug).
2. Check 6 (`~/.local/bin/hermes` mentions `hermes-agent`) keeps passing after a v0.22.2 first launch only
   because the new shim is `exec <checkout>/.hermes/bin/hermes "$@"` (the path still contains
   `hermes-agent`). Consider asserting it execs `~/.hermes/hermes-agent/.hermes/bin/hermes` or the old
   `venv/bin/python` explicitly.
3. Suggested additions: `FORK(launchctl-guard)` in `tests/_fixtures/live_system_guard.py`; exactly one
   `def _spawn_typescript(` in `agent/lsp/servers.py`; `FORK(opencode-zen-keyless)` present in
   `hermes_cli/auth.py`; the 2 `FORK(memory-capacity-guard)` hooks are still counted correctly (the second
   hook's tag now sits on its own comment line above the `current_entries` line).
