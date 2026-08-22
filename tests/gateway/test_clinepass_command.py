"""Tests for the gateway ``/clinepass`` handler's composition contract.

The handler owns no switching machinery: it must delegate the model switch
to ``_handle_model_command`` via a synthetic ``/model`` event, gate the
effort change on the switch actually landing (session override present),
and apply the effort through ``_apply_reasoning_selection``. These tests
pin that contract with instance-level stubs.
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.clinepass_command import CLINEPASS_LEVELS, DEFAULT_CLINEPASS_LEVEL


def _make_runner(switch_succeeds=True, switch_reply="switched!"):
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._model_events = []
    runner._reasoning_calls = []

    async def _fake_model_command(event):
        runner._model_events.append(event)
        if switch_succeeds:
            # Mirror the real handler: store the session override on success.
            model = event.get_command_args().split()[0]
            runner._session_model_overrides["sess"] = {"model": model}
        return switch_reply

    def _fake_apply_reasoning(session_key, platform_key, value, persist_global=False):
        runner._reasoning_calls.append((session_key, platform_key, value, persist_global))
        return f"effort set to {value}"

    runner._handle_model_command = _fake_model_command
    runner._apply_reasoning_selection = _fake_apply_reasoning
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda source: "sess"
    return runner


def _make_event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
    )


def test_level_delegates_model_switch_and_applies_effort():
    runner = _make_runner()
    reply = asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass high")))

    model, effort = CLINEPASS_LEVELS["high"]
    assert len(runner._model_events) == 1
    assert runner._model_events[0].text == f"/model {model} --provider cline"
    assert runner._reasoning_calls == [("sess", "telegram", effort, False)]
    assert "high" in reply and "switched!" in reply and f"effort set to {effort}" in reply


def test_bare_invocation_uses_default_level():
    runner = _make_runner()
    asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass")))

    model, effort = CLINEPASS_LEVELS[DEFAULT_CLINEPASS_LEVEL]
    assert runner._model_events[0].text == f"/model {model} --provider cline"
    assert runner._reasoning_calls[0][2] == effort


def test_global_flag_propagates_to_both_paths():
    runner = _make_runner()
    asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass max --global")))

    assert runner._model_events[0].text.endswith(" --provider cline --global")
    assert runner._reasoning_calls[0][3] is True


def test_failed_switch_returns_reply_and_skips_reasoning():
    runner = _make_runner(switch_succeeds=False, switch_reply="Error: no creds")
    reply = asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass low")))

    assert reply == "Error: no creds"
    assert runner._reasoning_calls == []


def test_unknown_level_short_circuits():
    runner = _make_runner()
    reply = asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass turbo")))

    assert reply.startswith("❌")
    assert runner._model_events == []
    assert runner._reasoning_calls == []
