"""Direct execution helper for the ``/aiuse`` slash command."""

from __future__ import annotations

import shutil
import subprocess


def run_aiuse_for_chat(*, timeout: float = 60.0) -> str:
    """Return the live ``aiuse --for-chat`` report without invoking an AI.

    The command is intentionally fixed-argument: ``/aiuse`` is an informational
    slash command, not a shell escape hatch.  ``-q`` keeps collector progress
    off the chat response while preserving the report on stdout.
    """
    executable = shutil.which("aiuse")
    if not executable:
        return "❌ `aiuse` is not installed or is not on PATH."

    try:
        result = subprocess.run(
            [executable, "--for-chat", "-q"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "❌ `aiuse` timed out while collecting usage data."
    except OSError as exc:
        return f"❌ Could not run `aiuse`: {exc}"

    output = (result.stdout or "").strip()
    if output:
        return output
    if result.returncode:
        detail = (result.stderr or "").strip()
        suffix = f" {detail}" if detail else ""
        return f"❌ `aiuse` failed (exit {result.returncode}).{suffix}"
    return "(aiuse returned no usage data.)"
