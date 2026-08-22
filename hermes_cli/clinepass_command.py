"""Shared logic for the ``/clinepass`` slash command.

``/clinepass`` switches the acting model to the ClinePass provider
(``cline``) at a chosen capability level, setting BOTH the model and the
reasoning effort in one step:

    /clinepass              — interactive level picker on platforms that
                              support one (Telegram/Discord/Matrix); the
                              balanced default (medium) on the CLI
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
    """Parsed ``/clinepass`` arguments.

    ``explicit`` is False when the user named no level (bare ``/clinepass``,
    with or without flags). Surfaces that can render an interactive picker
    use it to offer the level list instead of silently applying the default;
    surfaces that cannot fall back to ``level`` (the default level).
    """

    level: str
    model: str
    effort: str
    persist_global: bool
    error: str | None = None
    explicit: bool = True


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

    explicit = True
    if not levels:
        level = DEFAULT_CLINEPASS_LEVEL
        explicit = False
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
        level=level,
        model=model,
        effort=effort,
        persist_global=persist_global,
        explicit=explicit,
    )


def short_model(model: str) -> str:
    """Drop the shared ``cline-pass/`` namespace for compact UI labels."""
    return model.split("/", 1)[-1] if "/" in model else model


def level_label(level: str) -> str:
    """One-line ``level · model @ effort`` label for a picker button.

    Every level maps to a different model *and* effort, so the label has to
    carry both — a bare ``high`` tells the operator nothing about which model
    or how hard it will think. The ``cline-pass/`` prefix is dropped because
    it is identical for all five and would eat the button width that the
    distinguishing part needs.
    """
    model, effort = CLINEPASS_LEVELS[level]
    return f"{level} · {short_model(model)} @ {effort}"


def picker_choices(
    current_model: str = "", current_effort: str = ""
) -> list[dict]:
    """Choice dicts for the generic ``send_choice_picker`` adapter contract.

    ``is_current`` requires BOTH the model and the effort to match, since two
    levels (``high``/``xhigh``) share a model and differ only by effort.
    """
    choices = []
    for level, (model, effort) in CLINEPASS_LEVELS.items():
        choices.append(
            {
                "value": level,
                "label": level_label(level),
                "is_current": bool(current_model)
                and current_model == model
                and current_effort == effort,
            }
        )
    return choices


def picker_title(current_model: str = "", current_effort: str = "") -> str:
    """Title card shown above the picker (first line is the embed title on
    Discord, so it must stand alone)."""
    if current_model and current_model.startswith("cline-pass/"):
        current = f"`{current_model}` @ `{current_effort or 'default'}`"
    elif current_model:
        current = f"`{current_model}` (not a ClinePass model)"
    else:
        current = "_config default_"
    return (
        "🎚️ **ClinePass levels**\n"
        f"\n**Current:** {current}\n"
        f"\nPick a level (default is `{DEFAULT_CLINEPASS_LEVEL}`):"
    )
