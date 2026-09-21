# Upstream merge conflict map — v2026.9.14 (2026-09-21)

Merge of NousResearch/hermes-agent tag `v2026.9.14` (345cd2b057) into the fork
(pre-merge live `main` = `11e6f40d9a`). Merge base `fcbd1076a9` (the upstream side of the
previous merge `22fedc8a36`). Upstream range: 10,894 commits. Fork delta vs base: 70 files,
44 of them also touched upstream. Git reported 38 conflicted paths.

Merge commit `e630b28892`, follow-ups `0df6696664` (post-merge fixes), `2b8a139192`
(test reconciliation). Playbook: `docs/UPSTREAM-MERGE-PLAYBOOK.md`.

## Shallow-clone note

`.git/shallow` had grown to 20 entries and now listed the fork's own tips (`11e6f40d9a`,
`ae230f072e`, `c4fc319704`) as boundaries — `hermes update`'s check path does
`git fetch --depth 1` on a shallow repo and marks every fetched tip. `git merge-base` was
empty. Fix: back up `.git/shallow`, delete those three lines only (their parents exist
locally), merge, restore the backup byte-identical. No replace refs were needed this time.

## Per-feature decision table (operator instruction: switch to upstream where it now covers the feature)

| Fork feature | Upstream equivalent at v2026.9.14? | Decision | Test evidence |
|---|---|---|---|
| Operator-managed launchd label: helpers, restart/stop/start/status routing, `_get_service_pids` foreign pid, `launchd_install` guard | No (`--external-supervisor` only; no foreign-label discovery) | **kept ours**, re-implemented in upstream's `_cmd_start/_cmd_stop/_restart_all/_cmd_restart/_cmd_status` + `_stop_installed_service` layout; `_launchd_user_home()` helper restored (tests patch it) | `tests/hermes_cli/test_gateway_service.py` 131 passed / 5 skipped (TestForeignLaunchdGateway* 47 passed); live `hermes gateway status`/`restart` after promotion |
| Post-`hermes update` restart of the foreign launchd job | No | **kept ours**, moved to `hermes_cli/update_cmd_fleet.py::_restart_macos_launchd_gateways` (watchdog script updated to look there) | `test_update_restarts_foreign_launchd_job` passes |
| `should_restart_after_signal()` launchd clean-exit guard | No (`_resolve_gateway_exit_verdict` still exits 1 after signal) | **kept ours**, guard now in `gateway/run_shutdown.py`; `gateway/restart.py` def auto-merged | `tests/gateway/test_runner_startup_failures.py` (3 fork tests) pass |
| Tolerant MCP `tools/list` (malformed tool dropped, not the server) | No | **kept ours**, as new `tools/mcp_tolerant_list.py` hooked from `tools/mcp_tool_loop._ensure_mcp_loop` | no unit test existed; behavioural check after promotion (gateway log: dropped-tool warning, server still registers) |
| Telegram clipboard-copy button on split replies | No | **kept ours** (send + overflow-edit paths, `cc:` callback in upstream's prefix table) | `tests/gateway/test_telegram_clipboard_copy_button.py` passes |
| Telegram `interactive_resume = False` | Upstream added the `interactive_resume` adapter attribute but Telegram defaults to True | **kept ours** (fork preference on top of upstream's mechanism) | covered by adapter import; no dedicated test |
| `full_width` choice-picker rows (3 adapters + gateway picker) | No | **kept ours** (`_try_send_choice_picker` now in `gateway/slash_commands_model.py`) | `tests/gateway/test_choice_picker.py`, `test_telegram_choice_picker_rows.py` pass |
| Memory capacity guard (journal at 85%, overlay reads, pending queue, projection, cron consumer) | No (upstream split `MemoryStore` into `tools/memory_tool_store.py`; only per-turn consolidation-failure counting) | **kept ours**: `tools/memory_tool.py` taken as ours, upstream's five fixes reverse-ported (secure 0600/O_NOFOLLOW lock files, oversize-on-load warning #10877, empty-store batch refusal #103419, background-review delete gate #105921, PEP 562 plugin shim). Upstream's `memory_tool_store.py` is present but unused. | 172 memory/write-approval tests pass incl. upstream's 12 new ones; `tests/tools/test_memory_pending_queue.py` needed the journal to chmod its own dir (upstream no longer leaves HERMES_HOME at 0700) — **SUPERSEDED 2026-09-21: forward-ported; `tools/memory_tool*.py` now match upstream verbatim (2 tagged hooks) and the guard is the fork-only `tools/memory_capacity_guard.py`; see `docs/MEMORY_GUARD_FORWARD_PORT_2026-09-21.md`.** |
| MoA advisor cooldown | No | **kept ours** (`agent/moa_loop.py`, `hermes_cli/moa_config.py`) | `tests/agent/test_moa_reference_cooldown.py` (moved with upstream's `tests/run_agent`→`tests/agent` rename) passes |
| `discover_models: false` honoured by `validate_requested_model` | No (`_validate_custom` still probes) | **kept ours** as a new ladder rung `_validate_discovery_disabled` in `hermes_cli/models_validate.py` | `tests/hermes_cli/test_model_validation.py` passes |
| OpenCode Zen `x-api-key` header | No | **kept ours** in `hermes_cli/runtime_provider_custom.py::_apply_custom_provider_extras` (one site covers pool + named paths) | `tests/hermes_cli/test_opencode_zen_free_keyless.py` / `_policy.py` pass |
| ClinePass no-spend gate (`allow_paid_opencode_zen`) | No | **kept ours** (`resolve_runtime_provider` kwarg + `_enforce_opencode_zen_free_only`; `model_switch` passes `bool(st.explicit_provider)`) | 273 runtime-provider/model-switch/opencode tests pass |
| Truncation-marker guards (file_tools write/patch, compressor marker) | No (`...[truncated]` still emitted) | **kept ours** | `tests/tools/test_file_tools.py::TestTruncationPlaceholderGuard`, `tests/agent/test_context_compressor.py` pass |
| `/clinepass`, `/aiuse` (CLI + gateway + registry + Slack via-hermes) | No | **kept ours**; upstream's `_slash_handler` naming fallback dispatches both without a table entry | `test_clinepass_command.py` (cli+gateway), `test_aiuse_command.py`, `test_commands.py`, `test_slash_dispatch_table.py` (adapted), website doc parity test pass; `slack_native_slashes()` == 50 |
| `cline.bot` reasoning-param support | No | **kept ours**, moved to `agent/reasoning_params.py` | covered by clinepass gateway tests |
| `shutdown_forensics` supervisor labeling (launchd vs systemd vs pid1) | No | **kept ours** | `tests/gateway/test_shutdown_forensics.py` passes |
| Empty `tool_calls` list coercion (message_sanitization) | **Yes** — `agent/transports/chat_completions.py` strips `tool_calls=[]` on assistant messages | **switched to upstream**, fork patch dropped | `tests/agent/transports/test_chat_completions_empty_tool_calls.py`, `test_message_sanitization_policy.py`, `test_send_path_history_isolation.py` pass |
| `/status` prefers latest coherent per-call route (`get_latest_session_model_usage`) | **Yes** — `hermes_state_sessions.get_recent_session_model_route` (same query) + `_status_model_route` | **switched to upstream**, fork method + status patch dropped, upstream's test kept | `tests/gateway/test_status_command.py` passes |
| Drop `--replace` from launchd ProgramArguments (RCA 2026-08-08) | **Yes** — `_timestamped_stderr_gateway_command(external_supervisor=True)` drops it (#79048) | **switched to upstream**; fork's `test_launchd_plist_omits_replace_flag` kept as the RCA guard | passes |
| Dependency pin bumps (pyproject, uv.lock, lazy_deps, package-lock, desktop electron, google-workspace setup.py) | Upstream's Sep-14 pins supersede the fork's Aug-22 bumps | **switched to upstream** (never bump beyond the merged lockfile) | `test_google_workspace_setup_deps.py` passes after restoring upstream's `setup.py` |

## File → resolution (38 conflicted paths)

- `agent/context_compressor.py` — theirs + fork marker re-applied.
- `agent/message_sanitization.py` — theirs (feature switched to upstream).
- `agent/moa_loop.py` — theirs + cooldown re-applied.
- `cli.py` — theirs (dispatch table; fork commands resolve by naming fallback).
- `gateway/run.py` — theirs (exit verdict moved to `gateway/run_shutdown.py`, guard re-applied there).
- `gateway/shutdown_forensics.py` — theirs + supervisor labeling re-applied.
- `gateway/slash_commands.py` — theirs (status patch dropped; clinepass/aiuse re-applied in `slash_commands_model.py` / `slash_commands_status.py`).
- `hermes_cli/cli_commands_mixin.py`, `hermes_cli/commands.py` — theirs + `/clinepass`, `/aiuse` re-added (`/aiuse` CLI handler in `cli_info_mixin.py`; Slack set in `commands_platforms.py`).
- `hermes_cli/gateway.py` — theirs + foreign-launchd helpers and routing re-applied (see table).
- `hermes_cli/moa_config.py` — theirs + cooldown coercion.
- `hermes_cli/model_switch.py`, `hermes_cli/runtime_provider.py` — theirs + no-spend gate; Zen header in `runtime_provider_custom.py`.
- `hermes_cli/models.py` — theirs (discover_models rung lives in `models_validate.py`).
- `hermes_cli/update_cmd.py` — theirs (foreign restart re-applied in `update_cmd_fleet.py`).
- `hermes_cli/write_approval_commands.py` — ours (journal/evicted/migrate/import-fallback subcommands) on upstream's file.
- `hermes_state.py` — theirs (status feature switched to upstream).
- `plugins/platforms/{discord,matrix,telegram}/adapter.py` — theirs + `full_width`; Telegram also clipboard button + `interactive_resume`.
- `pyproject.toml`, `uv.lock`, `tools/lazy_deps.py`, `package-lock.json`, `apps/desktop/package.json` — theirs.
- `run_agent.py` — theirs (cline.bot moved to `agent/reasoning_params.py`).
- `tests/gateway/test_choice_picker.py` — union (upstream's values list + fork's `full_width` assert).
- `tests/gateway/test_runner_startup_failures.py` — union of imports.
- `tests/gateway/test_status_command.py` — theirs.
- `tests/hermes_cli/test_gateway_service.py` — fork's `--replace` pinning test kept, upstream comment change taken.
- `tests/tools/test_file_tools.py` — both classes kept.
- `tools/file_tools.py`, `tools/mcp_tool.py` — theirs + fork guards (mcp: new `tools/mcp_tolerant_list.py`).
- `tools/memory_tool.py` — ours + upstream fixes reverse-ported (**superseded 2026-09-21**: now upstream verbatim + 2 `FORK(memory-capacity-guard)` hooks, guard in `tools/memory_capacity_guard.py`; see `docs/MEMORY_GUARD_FORWARD_PORT_2026-09-21.md`).
- `tools/write_approval.py` — theirs + MEMORY-journal branches re-applied.
- `docs/handoffs/*`, `docs/rca-launchd-*.md` — kept under `docs/` (upstream deleted `docs/`; git had auto-moved them under `website/docs/developer-guide/`).
- `tests/run_agent/test_moa_reference_cooldown.py` → `tests/agent/` (upstream dir rename).

## Test verdict

Chunked full suite (9 chunks, ~52,300 tests): raw 268 failures. Re-run isolated on the merged tree
(`-n 4`): 97 consistent. Same set on a pristine `v2026.9.14` worktree with the same venv: 92.
Merge-caused = 5, all fixed in `2b8a139192` (google-workspace `setup.py` pins ×4, slash-command
doc parity ×1); one more found earlier (`test_db_file_created_with_wal_and_restrictive_permissions`,
fixed in `0df6696664`) and `_launchd_user_home` (same commit). The 92 environmental failures are
identical on pristine: web dashboard tests (`_methods` SimpleNamespace), update autostash/self-lock
git fixtures, dashboard param clamps, PTB polling-progress plugin import, tui_gateway projects RPC,
voice/sounddevice, install.sh node/termux, cross-VM WAL, `/tmp`→`/private/tmp` path asserts in
file_tools, code kernel, cua overlay, contributor map, etc.
