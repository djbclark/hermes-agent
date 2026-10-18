"""Regression tests for the OpenCode Zen free-only safety policy."""

from types import SimpleNamespace

import pytest

from hermes_cli.model_cost_guard import is_free_model, opencode_zen_policy_error


class _Info:
    def __init__(self, input_cost, output_cost, cache_read=0, cache_write=0):
        self.cost_input = input_cost
        self.cost_output = output_cost
        self.cost_cache_read = cache_read
        self.cost_cache_write = cache_write

    def has_cost_data(self):
        return True


def test_zero_cost_zen_model_is_free():
    assert is_free_model(
        "deepseek-v4-flash-free",
        provider="opencode-zen",
        model_info=_Info(0, 0, 0, 0),
    )


def test_paid_zen_model_is_not_free():
    assert not is_free_model(
        "gpt-5.6-luna",
        provider="opencode-zen",
        model_info=_Info(0.2, 1.2, 0.02, 0.25),
    )


def test_unknown_zen_pricing_fails_closed():
    assert not is_free_model("unknown-model", provider="opencode-zen", model_info=None)


def test_policy_error_is_actionable():
    message = opencode_zen_policy_error("gpt-5.6-luna")
    assert "free-only policy" in message
    assert "allow_paid_opencode_zen" in message


def test_runtime_provider_blocks_paid_zen_default(monkeypatch):
    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_requested_provider",
        lambda requested=None: "opencode-zen",
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "gpt-5.6-luna"}},
    )
    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.is_free_model",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(ValueError, match="free-only policy"):
        runtime_provider.resolve_runtime_provider(
            requested="opencode-zen", target_model="gpt-5.6-luna"
        )


def test_runtime_provider_allows_explicit_paid_override(monkeypatch):
    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_requested_provider",
        lambda requested=None: "opencode-zen",
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "gpt-5.6-luna"}},
    )
    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.is_free_model",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        runtime_provider,
        "_resolve_named_custom_runtime",
        lambda **kwargs: {
            "provider": "opencode-zen",
            "api_mode": "codex_responses",
            "base_url": "https://opencode.ai/zen/v1",
            "api_key": "[REDACTED]",
        },
    )

    result = runtime_provider.resolve_runtime_provider(
        requested="opencode-zen",
        target_model="gpt-5.6-luna",
        allow_paid_opencode_zen=True,
    )
    assert result["provider"] == "opencode-zen"
