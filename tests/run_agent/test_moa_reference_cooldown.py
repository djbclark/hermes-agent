"""Reference-advisor cooldown: a failing advisor is skipped for
``reference_cooldown_seconds`` so an exhausted free/paid tier doesn't make every
subsequent turn re-walk the timeout + fallback chain on a dead endpoint.

Covers the "fail fast, ignore for ~10 min" behavior requested for the
``free-advice`` preset (free advisory references + paid aggregator).
"""

from types import SimpleNamespace


def _response(content="done", *, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None, model="fake-model")


def _cooldown_config(home, cooldown="600"):
    home.mkdir()
    (home / "config.yaml").write_text(
        f"""
moa:
  default_preset: review
  presets:
    review:
      reference_cooldown_seconds: {cooldown}
      reference_models:
        - provider: opencode-zen-free
          model: deepseek-v4-flash-free
      aggregator:
        provider: deepseek
        model: deepseek-v4-pro
""".strip(),
        encoding="utf-8",
    )


def _install_failing_llm(monkeypatch, ref_attempts):
    def fake_call_llm(**kwargs):
        if kwargs.get("task") == "moa_reference":
            ref_attempts.append(kwargs.get("model"))
            raise RuntimeError("429 FreeUsageLimitError")
        return _response("acted")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)


def test_failed_reference_is_skipped_during_cooldown(monkeypatch, tmp_path):
    from agent import moa_loop

    # Isolate the module-level cooldown registry from other tests.
    monkeypatch.setattr(moa_loop, "_REFERENCE_COOLDOWN_UNTIL", {})

    home = tmp_path / ".hermes"
    _cooldown_config(home, cooldown="600")
    monkeypatch.setenv("HERMES_HOME", str(home))

    ref_attempts = []
    _install_failing_llm(monkeypatch, ref_attempts)

    facade = moa_loop.MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "turn one"}], tools=[{"type": "function"}])

    # First turn attempted the advisor once; it failed.
    assert ref_attempts == ["deepseek-v4-flash-free"]

    # A new turn inside the cooldown window must NOT re-attempt the advisor.
    facade.create(messages=[{"role": "user", "content": "turn two"}], tools=[{"type": "function"}])
    assert ref_attempts == ["deepseek-v4-flash-free"]

    # After the cooldown elapses, the advisor is attempted again.
    key = ("opencode-zen-free", "deepseek-v4-flash-free")
    assert key in moa_loop._REFERENCE_COOLDOWN_UNTIL
    moa_loop._REFERENCE_COOLDOWN_UNTIL[key] = 0.0  # simulate expiry
    facade.create(messages=[{"role": "user", "content": "turn three"}], tools=[{"type": "function"}])
    assert ref_attempts == ["deepseek-v4-flash-free", "deepseek-v4-flash-free"]


def test_zero_cooldown_disables_skip(monkeypatch, tmp_path):
    from agent import moa_loop

    monkeypatch.setattr(moa_loop, "_REFERENCE_COOLDOWN_UNTIL", {})

    home = tmp_path / ".hermes"
    _cooldown_config(home, cooldown="0")
    monkeypatch.setenv("HERMES_HOME", str(home))

    ref_attempts = []
    _install_failing_llm(monkeypatch, ref_attempts)

    facade = moa_loop.MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "turn one"}], tools=[{"type": "function"}])
    facade.create(messages=[{"role": "user", "content": "turn two"}], tools=[{"type": "function"}])

    # cooldown=0 means every turn still attempts the advisor.
    assert ref_attempts == ["deepseek-v4-flash-free", "deepseek-v4-flash-free"]
