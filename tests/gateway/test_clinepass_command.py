"""Tests for the gateway ``/clinepass`` handler's composition contract.

The handler owns no switching machinery: it must delegate the model switch
to ``_handle_model_command`` via a synthetic ``/model`` event, gate the
effort change on the switch actually landing (session override present),
and apply the effort through ``_apply_reasoning_selection``. Bare
``/clinepass`` must offer the interactive level picker rather than silently
applying the default. These tests pin that contract with instance-level stubs.
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.clinepass_command import CLINEPASS_LEVELS, DEFAULT_CLINEPASS_LEVEL


def _make_runner(switch_succeeds=True, switch_reply="switched!", picker_available=False):
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
    runner._resolve_session_reasoning_config = lambda **kw: {"effort": "xhigh"}

    runner._pickers = []

    async def _fake_picker(event, session_key, title, choices, on_choice_selected):
        runner._pickers.append(
            {
                "title": title,
                "choices": choices,
                "on_choice_selected": on_choice_selected,
            }
        )
        return picker_available

    runner._try_send_choice_picker = _fake_picker
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


def test_bare_invocation_opens_picker_and_changes_nothing_yet():
    runner = _make_runner(picker_available=True)
    reply = asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass")))

    # Picker sent — the adapter owns the response, and nothing is applied
    # until the operator actually taps a level.
    assert reply is None
    assert runner._model_events == []
    assert runner._reasoning_calls == []

    choices = runner._pickers[0]["choices"]
    assert [c["value"] for c in choices] == list(CLINEPASS_LEVELS)
    # Every label carries the level AND what it maps to (model @ effort).
    for level, (model, effort) in CLINEPASS_LEVELS.items():
        label = next(c["label"] for c in choices if c["value"] == level)
        assert label.startswith(level)
        assert model.split("/")[-1] in label
        assert f"@ {effort}" in label


def test_picker_selection_applies_the_level():
    runner = _make_runner(picker_available=True)
    asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass")))

    on_selected = runner._pickers[0]["on_choice_selected"]
    reply = asyncio.run(on_selected("chat-1", "max"))

    model, effort = CLINEPASS_LEVELS["max"]
    assert runner._model_events[0].text == f"/model {model} --provider cline"
    assert runner._reasoning_calls == [("sess", "telegram", effort, False)]
    assert "max" in reply


def test_picker_selection_preserves_global_flag():
    runner = _make_runner(picker_available=True)
    asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass --global")))

    on_selected = runner._pickers[0]["on_choice_selected"]
    asyncio.run(on_selected("chat-1", "low"))

    assert runner._model_events[0].text.endswith(" --provider cline --global")
    assert runner._reasoning_calls[0][3] is True


def test_bare_without_picker_falls_back_to_the_level_table():
    runner = _make_runner(picker_available=False)
    reply = asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass")))

    # No picker on this platform: show the table, do not silently switch.
    assert runner._model_events == []
    assert runner._reasoning_calls == []
    for level in CLINEPASS_LEVELS:
        assert level in reply
    assert DEFAULT_CLINEPASS_LEVEL in reply


def test_help_subcommand_returns_the_level_table():
    runner = _make_runner()
    reply = asyncio.run(runner._handle_clinepass_command(_make_event("/clinepass help")))

    assert runner._model_events == []
    for level, (model, effort) in CLINEPASS_LEVELS.items():
        assert model in reply


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
