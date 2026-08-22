"""Tests for the direct /aiuse command execution path."""

from __future__ import annotations

import subprocess

from hermes_cli.aiuse_command import run_aiuse_for_chat
from hermes_cli.commands import COMMAND_REGISTRY, resolve_command


def test_aiuse_is_a_gateway_command_and_resolves() -> None:
    command = next(item for item in COMMAND_REGISTRY if item.name == "aiuse")
    assert command.busy_policy == "dispatch"
    assert not command.cli_only
    assert not command.gateway_only
    assert resolve_command("aiuse") is command


def test_run_aiuse_uses_fixed_for_chat_arguments(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr("hermes_cli.aiuse_command.shutil.which", lambda name: "/bin/aiuse")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="- live usage\n", stderr="")

    monkeypatch.setattr("hermes_cli.aiuse_command.subprocess.run", fake_run)

    assert run_aiuse_for_chat() == "- live usage"
    assert calls == [
        (
            ["/bin/aiuse", "--for-chat", "-q"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 60.0,
                "check": False,
            },
        )
    ]


def test_run_aiuse_reports_timeout(monkeypatch) -> None:
    monkeypatch.setattr("hermes_cli.aiuse_command.shutil.which", lambda name: "/bin/aiuse")

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr("hermes_cli.aiuse_command.subprocess.run", fake_run)

    assert "timed out" in run_aiuse_for_chat()


def test_run_aiuse_reports_nonzero_with_stderr(monkeypatch) -> None:
    monkeypatch.setattr("hermes_cli.aiuse_command.shutil.which", lambda name: "/bin/aiuse")
    monkeypatch.setattr(
        "hermes_cli.aiuse_command.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 2, stdout="", stderr="collector unavailable"
        ),
    )

    assert run_aiuse_for_chat() == "❌ `aiuse` failed (exit 2). collector unavailable"
