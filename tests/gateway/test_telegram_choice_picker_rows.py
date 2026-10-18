"""Row layout of the Telegram choice-picker inline keyboard.

The generic picker packs two buttons per row by default; a picker whose
labels are long (e.g. /clinepass's "level · model @ effort") passes
``full_width=True`` and gets one button per row instead.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

import plugins.platforms.telegram.adapter as tg_adapter_mod
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


class _FakeButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data


class _FakeMarkup:
    def __init__(self, rows):
        self.rows = rows


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter._reply_to_mode = None
    adapter._reply_to_message_id_for_send = lambda *a, **k: None
    adapter._thread_kwargs_for_send = lambda *a, **k: {}
    adapter._link_preview_kwargs = lambda: {}
    adapter.format_message = lambda text: text
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=SimpleNamespace(message_id=42)
    )
    return adapter


def _choices(n):
    return [
        {"value": f"v{i}", "label": f"label {i}", "is_current": i == 0}
        for i in range(n)
    ]


async def _sent_rows(adapter, **kwargs):
    result = await adapter.send_choice_picker(
        chat_id="1",
        title="pick one",
        choices=_choices(5),
        session_key="sess",
        on_choice_selected=AsyncMock(),
        **kwargs,
    )
    assert result.success
    markup = adapter._send_message_with_thread_fallback.call_args.kwargs["reply_markup"]
    return markup.rows


@pytest.mark.asyncio
async def test_default_layout_is_two_buttons_per_row(monkeypatch):
    monkeypatch.setattr(tg_adapter_mod, "InlineKeyboardButton", _FakeButton)
    monkeypatch.setattr(tg_adapter_mod, "InlineKeyboardMarkup", _FakeMarkup)
    rows = await _sent_rows(_make_adapter())

    assert [len(r) for r in rows] == [2, 2, 1]


@pytest.mark.asyncio
async def test_full_width_puts_each_button_on_its_own_row(monkeypatch):
    monkeypatch.setattr(tg_adapter_mod, "InlineKeyboardButton", _FakeButton)
    monkeypatch.setattr(tg_adapter_mod, "InlineKeyboardMarkup", _FakeMarkup)
    rows = await _sent_rows(_make_adapter(), full_width=True)

    assert [len(r) for r in rows] == [1, 1, 1, 1, 1]
    # Order and labels are unchanged — full width only affects the layout.
    assert [r[0].text for r in rows] == [
        "✓ label 0", "label 1", "label 2", "label 3", "label 4",
    ]
