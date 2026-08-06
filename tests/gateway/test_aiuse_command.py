"""Tests for the direct gateway /aiuse command."""

import subprocess
from unittest.mock import patch

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli.commands import resolve_command


class _Event:
    def __init__(self, args: str = ""):
        self._args = args

    def get_command_args(self) -> str:
        return self._args


@pytest.mark.asyncio
async def test_aiuse_is_a_gateway_command():
    command = resolve_command("aiuse")
    assert command is not None
    assert command.name == "aiuse"
    assert command.gateway_only is True


@pytest.mark.asyncio
async def test_aiuse_returns_report_for_alert_exit_code():
    adapter = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    completed = subprocess.CompletedProcess(
        args=["aiuse", "--for-chat", "-q"],
        returncode=2,
        stdout="report with active alerts\n",
        stderr="",
    )
    with patch("gateway.slash_commands.shutil.which", return_value="/usr/local/bin/aiuse"), \
         patch("gateway.slash_commands.subprocess.run", return_value=completed) as run:
        result = await adapter._handle_aiuse_command(_Event())

    assert result == "report with active alerts"
    assert run.call_args.args[0] == ["/usr/local/bin/aiuse", "--for-chat", "-q"]


@pytest.mark.asyncio
async def test_aiuse_rejects_arguments():
    adapter = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    with patch("gateway.slash_commands.shutil.which") as which:
        result = await adapter._handle_aiuse_command(_Event("unexpected"))
    assert result == "Usage: /aiuse"
    which.assert_not_called()


@pytest.mark.asyncio
async def test_aiuse_reports_unavailable_executable():
    adapter = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    with patch("gateway.slash_commands.shutil.which", return_value=None):
        result = await adapter._handle_aiuse_command(_Event())
    assert "not available" in result


@pytest.mark.asyncio
async def test_aiuse_reports_empty_unexpected_failure():
    adapter = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    completed = subprocess.CompletedProcess(
        args=["aiuse", "--for-chat", "-q"],
        returncode=1,
        stdout="",
        stderr="collector failed",
    )
    with patch("gateway.slash_commands.shutil.which", return_value="/usr/local/bin/aiuse"), \
         patch("gateway.slash_commands.subprocess.run", return_value=completed):
        result = await adapter._handle_aiuse_command(_Event())
    assert "did not produce a report" in result
