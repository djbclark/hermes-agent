"""Tests for the /clinepass level parser and mapping invariants."""

from hermes_cli.clinepass_command import (
    CLINEPASS_LEVELS,
    CLINEPASS_PROVIDER,
    DEFAULT_CLINEPASS_LEVEL,
    level_label,
    parse_clinepass_args,
    picker_choices,
    picker_title,
    short_model,
    status_text,
    usage_text,
)

# ClinePass forwards to OpenRouter; this is OpenRouter's reasoning_effort
# enum (verified live 2026-08-22 via a 400 on an invalid option).
_CLINEPASS_EFFORT_ENUM = {"max", "xhigh", "high", "medium", "low", "minimal", "none"}


def test_provider_is_cline():
    assert CLINEPASS_PROVIDER == "cline"


def test_levels_cover_the_requested_ladder_in_order():
    assert list(CLINEPASS_LEVELS) == ["low", "medium", "high", "xhigh", "max"]


def test_every_level_maps_to_catalog_model_and_valid_effort():
    for level, (model, effort) in CLINEPASS_LEVELS.items():
        assert model.startswith("cline-pass/"), (level, model)
        assert effort in _CLINEPASS_EFFORT_ENUM, (level, effort)


def test_default_level_is_a_known_level():
    assert DEFAULT_CLINEPASS_LEVEL in CLINEPASS_LEVELS


def test_bare_invocation_resolves_default_level():
    request = parse_clinepass_args("")
    assert request.error is None
    assert request.level == DEFAULT_CLINEPASS_LEVEL
    assert (request.model, request.effort) == CLINEPASS_LEVELS[DEFAULT_CLINEPASS_LEVEL]
    assert request.persist_global is False


def test_each_level_parses_to_its_mapping():
    for level, (model, effort) in CLINEPASS_LEVELS.items():
        request = parse_clinepass_args(level)
        assert request.error is None
        assert (request.level, request.model, request.effort) == (level, model, effort)


def test_level_parsing_is_case_insensitive():
    request = parse_clinepass_args("HIGH")
    assert request.error is None
    assert request.level == "high"


def test_global_flag_with_and_without_level():
    assert parse_clinepass_args("--global").persist_global is True
    assert parse_clinepass_args("--global").level == DEFAULT_CLINEPASS_LEVEL
    request = parse_clinepass_args("max --global")
    assert request.persist_global is True
    assert request.level == "max"


def test_session_flag_is_accepted_noop():
    request = parse_clinepass_args("low --session")
    assert request.error is None
    assert request.level == "low"
    assert request.persist_global is False


def test_unknown_level_returns_error():
    request = parse_clinepass_args("ludicrous")
    assert request.error is not None
    assert "ludicrous" in request.error
    assert usage_text() in request.error


def test_multiple_levels_return_error():
    assert parse_clinepass_args("low high").error is not None


def test_status_text_lists_every_level():
    text = status_text()
    for level, (model, _effort) in CLINEPASS_LEVELS.items():
        assert level in text
        assert model in text


def test_command_is_registered():
    from hermes_cli.commands import COMMAND_REGISTRY

    matches = [c for c in COMMAND_REGISTRY if c.name == "clinepass"]
    assert len(matches) == 1
    cmd = matches[0]
    assert not cmd.cli_only and not cmd.gateway_only
    assert set(cmd.subcommands) == set(CLINEPASS_LEVELS)


def test_bare_invocation_is_not_explicit():
    """Bare /clinepass must be distinguishable from an explicit level so
    picker-capable surfaces can offer the list instead of applying a default."""
    assert parse_clinepass_args("").explicit is False
    assert parse_clinepass_args("  --global ").explicit is False
    assert parse_clinepass_args("medium").explicit is True


def test_short_model_drops_the_shared_namespace():
    assert short_model("cline-pass/kimi-k3") == "kimi-k3"
    assert short_model("kimi-k3") == "kimi-k3"


def test_level_label_carries_level_model_and_effort():
    for level, (model, effort) in CLINEPASS_LEVELS.items():
        label = level_label(level)
        assert label.startswith(level)
        assert short_model(model) in label
        assert label.endswith(f"@ {effort}")
        assert "cline-pass/" not in label


def test_picker_choices_cover_every_level_in_order():
    choices = picker_choices()
    assert [c["value"] for c in choices] == list(CLINEPASS_LEVELS)
    assert all(c["is_current"] is False for c in choices)


def test_picker_current_needs_both_model_and_effort():
    """high and xhigh share a model — effort is what separates them."""
    model, _ = CLINEPASS_LEVELS["xhigh"]
    choices = {c["value"]: c["is_current"] for c in picker_choices(model, "xhigh")}
    assert choices["xhigh"] is True
    assert choices["high"] is False

    # Same model at neither level's effort marks nothing current.
    assert not any(picker_choices(model, "low")[i]["is_current"] for i in range(5))


def test_picker_title_reports_current_and_default():
    assert DEFAULT_CLINEPASS_LEVEL in picker_title()
    assert "cline-pass/kimi-k3" in picker_title("cline-pass/kimi-k3", "high")
    assert "not a ClinePass model" in picker_title("gpt-5.5", "high")
