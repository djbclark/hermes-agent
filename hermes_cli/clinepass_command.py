"""Shared logic for the ``/clinepass`` slash command.

``/clinepass`` switches the acting model to the ClinePass provider
(``cline``) at a chosen capability level, setting BOTH the model and the
reasoning effort in one step:

    /clinepass              — balanced default (same as /clinepass medium)
    /clinepass low          — fastest / cheapest
    /clinepass medium       — balanced daily driver
    /clinepass high         — flagship model, high effort
    /clinepass xhigh        — flagship model, deeper reasoning
    /clinepass max          — strongest model at maximum effort
    /clinepass <level> --global   — persist model + effort to config.yaml

The command is a thin composition of the existing ``/model`` and
``/reasoning`` plumbing — it owns no switching machinery of its own, only
the level → (model, effort) mapping. Session-scoped by default, exactly
like ``/model``.

Level mapping (validated live against api.cline.bot on 2026-08-22 — every
catalog model answered the probe correctly; latency, cost and whether the
model actually modulates on reasoning_effort drove these choices):

    low     cline-pass/deepseek-v4-flash @ low     ~3s, cheapest paid tier
    medium  cline-pass/deepseek-v4-pro   @ medium  quality step up, still fast
    high    cline-pass/kimi-k3           @ high    flagship; effort-responsive
    xhigh   cline-pass/kimi-k3           @ xhigh   same flagship, deeper
    max     cline-pass/qwen3.7-max       @ max     strongest + priciest

ClinePass forwards to OpenRouter, whose reasoning_effort enum is exactly
``max|xhigh|high|medium|low|minimal|none`` — Hermes's own levels minus
``ultra``, so every level here passes through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

CLINEPASS_PROVIDER = "cline"

# Ordered lowest → highest capability. Values are (model_id, effort).
CLINEPASS_LEVELS: dict[str, tuple[str, str]] = {
    "low": ("cline-pass/deepseek-v4-flash", "low"),
    "medium": ("cline-pass/deepseek-v4-pro", "medium"),
    "high": ("cline-pass/kimi-k3", "high"),
    "xhigh": ("cline-pass/kimi-k3", "xhigh"),
    "max": ("cline-pass/qwen3.7-max", "max"),
}

DEFAULT_CLINEPASS_LEVEL = "medium"


@dataclass(frozen=True)
class ClinePassRequest:
    """Parsed ``/clinepass`` arguments."""

    level: str
    model: str
    effort: str
    persist_global: bool
    error: str | None = None


def usage_text() -> str:
    """One-line usage summary shared by both surfaces' error paths."""
    levels = "|".join(CLINEPASS_LEVELS)
    return f"Usage: /clinepass [{levels}] [--global]"


def status_text() -> str:
    """Multi-line level table for `/clinepass help`-style output."""
    lines = [usage_text(), ""]
    for level, (model, effort) in CLINEPASS_LEVELS.items():
        marker = " (default)" if level == DEFAULT_CLINEPASS_LEVEL else ""
        lines.append(f"  {level:<7} {model} @ {effort}{marker}")
    return "\n".join(lines)


def parse_clinepass_args(raw: str) -> ClinePassRequest:
    """Parse ``/clinepass`` arguments into a resolved request.

    Bare ``/clinepass`` resolves to ``DEFAULT_CLINEPASS_LEVEL``. Unknown
    levels return a request with ``error`` set (and the default level's
    mapping as inert placeholders); callers must check ``error`` first.
    """
    tokens = (raw or "").strip().lower().split()
    persist_global = "--global" in tokens
    # --session accepted as an explicit no-op for parity with /model.
    levels = [t for t in tokens if t not in ("--global", "--session")]

    if not levels:
        level = DEFAULT_CLINEPASS_LEVEL
    elif len(levels) == 1 and levels[0] in CLINEPASS_LEVELS:
        level = levels[0]
    else:
        bad = " ".join(levels)
        model, effort = CLINEPASS_LEVELS[DEFAULT_CLINEPASS_LEVEL]
        return ClinePassRequest(
            level=DEFAULT_CLINEPASS_LEVEL,
            model=model,
            effort=effort,
            persist_global=persist_global,
            error=f"Unknown level '{bad}'. {usage_text()}",
        )

    model, effort = CLINEPASS_LEVELS[level]
    return ClinePassRequest(
        level=level, model=model, effort=effort, persist_global=persist_global
    )
