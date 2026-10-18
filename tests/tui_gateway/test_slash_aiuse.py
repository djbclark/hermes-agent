"""/aiuse from slash.exec must not spawn the slash worker.

Desktop's default WS timeout is 30s and the worker pipe is 45s; live
``aiuse --for-chat`` collection routinely exceeds both. Answer from
``_live_slash_command_output`` instead.
"""

from __future__ import annotations

from unittest.mock import patch

from tui_gateway import server


def test_live_aiuse_bypasses_slash_worker():
    with patch(
        "hermes_cli.aiuse_command.run_aiuse_for_chat",
        return_value="- live usage",
    ) as run:
        out = server._live_slash_command_output("sid", None, "aiuse", "")

    assert out == "- live usage"
    run.assert_called_once_with()
