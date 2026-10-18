#!/usr/bin/env bash
# §4 fork-feature checklist: each line "file|literal"; prints PRESENT/MISSING.
cd "$1" || exit 2
miss=0
while IFS='|' read -r f pat; do
  [ -z "$f" ] && continue
  if [ -f "$f" ] && ggrep -qF -- "$pat" "$f"; then echo "PRESENT  $f :: $pat"; else echo "MISSING  $f :: $pat"; miss=$((miss+1)); fi
done <<'LIST'
hermes_cli/gateway.py|def _parse_launchd_label_for_pid
hermes_cli/gateway.py|def _find_foreign_launchd_gateway(
hermes_cli/gateway.py|def _restart_foreign_launchd_gateway
hermes_cli/gateway.py|def _find_foreign_launchd_gateway_plist
hermes_cli/gateway.py|def _stop_foreign_launchd_gateway
hermes_cli/gateway.py|def _start_foreign_launchd_gateway
hermes_cli/gateway.py|def _launchd_user_home
hermes_cli/gateway.py|_restart_foreign_launchd_gateway(*foreign)
hermes_cli/gateway.py|_stop_foreign_launchd_gateway(*foreign)
hermes_cli/gateway.py|_start_foreign_launchd_gateway(*foreign_plist)
hermes_cli/gateway.py|Supervised by launchd job
hermes_cli/gateway_launchd.py|operator-managed gateway LaunchAgent already exists
hermes_cli/gateway.py|pids.add(foreign[2])
hermes_cli/update_cmd_fleet.py|_restart_foreign_launchd_gateway(f_label, f_domain, f_pid)
gateway/run_shutdown.py|should_restart_after_signal()
gateway/restart.py|def should_restart_after_signal
tools/mcp_tolerant_list.py|def install_tolerant_list_tools
tools/mcp_tool_loop.py|install_tolerant_list_tools()
plugins/platforms/telegram/adapter.py|def _register_clipboard_copy
plugins/platforms/telegram/adapter.py|"cc:"
plugins/platforms/telegram/adapter.py|interactive_resume = False
plugins/platforms/telegram/adapter.py|full_width
plugins/platforms/discord/adapter.py|full_width
plugins/platforms/matrix/adapter.py|full_width
gateway/slash_commands_model.py|full_width
plugins/platforms/telegram/adapter.py|def _clarify_button_label
tools/memory_capacity_guard.py|def apply_with_capacity
tools/memory_capacity_guard.py|MEMORY_QUEUE_PCT
tools/memory_capacity_guard.py|class GuardedMemoryStore
tools/memory_tool.py|GuardedMemoryStore as MemoryStore
tools/memory_tool.py|store.entries_for_read(target),  # FORK(memory-capacity-guard)
tools/write_approval.py|_pq.enqueue(
agent/moa_loop.py|_record_reference_cooldown
hermes_cli/models_validate.py|_validate_discovery_disabled
hermes_cli/runtime_provider_custom.py|"x-api-key"
hermes_cli/runtime_provider.py|allow_paid_opencode_zen
hermes_cli/model_cost_guard.py|is_free_model
tools/file_tools.py|_find_truncation_placeholder(_extract_v4a_added_content(patch))
tools/file_tools.py|_find_truncation_placeholder(content)
tools/truncation_markers.py|def
hermes_cli/cli_commands_mixin.py|clinepass
hermes_cli/cli_info_mixin.py|aiuse
gateway/slash_commands_model.py|clinepass
gateway/slash_commands_status.py|aiuse
hermes_cli/commands.py|"clinepass"
hermes_cli/commands.py|"aiuse"
hermes_cli/commands_platforms.py|"aiuse", "clinepass"
hermes_cli/clinepass_command.py|def
hermes_cli/aiuse_command.py|def
agent/reasoning_params.py|"cline.bot"
gateway/shutdown_forensics.py|ctx["supervisor"]
hermes_cli/web_server_config.py|def _stringify_unsafe_ints
acp_adapter/server.py|custom
tools/mcp_tool.py|_MCP_HTTP_AVAILABLE
apps/desktop/src/lib/desktop-slash-commands.ts|AIUSE_SLASH_TIMEOUT_MS
apps/desktop/src/app/session/hooks/use-prompt-actions/slash.ts|timeoutMs: surface?.kind === 'exec'
tui_gateway/methods_slash.py|aiuse
agent/lsp/servers.py|_PYRIGHT_BINS
agent/lsp/servers.py|_server("ruff"
agent/lsp/servers.py|_server("marksman"
agent/lsp/servers.py|_server("taplo"
agent/lsp/servers.py|def _discover_tsserver_js
agent/lsp/servers.py|RUST_ANALYZER_WAIT_TIMEOUT
agent/lsp/manager.py|def _matching_servers
hermes_cli/opencode_zen_keyless.py|def
plugins/model-providers/opencode-free/__init__.py|def
hermes_cli/providers.py|"opencode-free": "OpenCode Free"
tools/memory_pending_queue.py|def enqueue
tools/memory_projection.py|def
tools/memory_pending_migration.py|def
cron/scripts/memory_journal_consumer.py|def
hermes_cli/write_approval_commands.py|journal
LIST
echo "missing=$miss"
