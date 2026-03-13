from __future__ import annotations

import importlib
import os

import zerg.models_config as models_config
from zerg.services import oikos_react_engine


def _reload_models_config() -> None:
    importlib.reload(models_config)


def test_hosted_profile_summary_update_routes_to_openrouter(monkeypatch):
    original_profile = os.environ.get("MODELS_PROFILE")
    original_key = os.environ.get("OPENROUTER_API_KEY")

    monkeypatch.setenv("MODELS_PROFILE", "hosted")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    _reload_models_config()

    try:
        model_id, provider, key_env = models_config.validate_use_case_llm_config("summary_update")
        assert model_id == "x-ai/grok-4.1-fast"
        assert provider == models_config.ModelProvider.OPENROUTER
        assert key_env == "OPENROUTER_API_KEY"
    finally:
        if original_profile is None:
            monkeypatch.delenv("MODELS_PROFILE", raising=False)
        else:
            monkeypatch.setenv("MODELS_PROFILE", original_profile)
        if original_key is None:
            monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        else:
            monkeypatch.setenv("OPENROUTER_API_KEY", original_key)
        _reload_models_config()


def test_openrouter_make_llm_uses_openrouter_key_and_base_url(monkeypatch):
    captured: dict[str, object] = {}

    class FakeOpenAIChat:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def bind_tools(self, tools, tool_choice=None):
            captured["tools"] = tools
            captured["tool_choice"] = tool_choice
            return self

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(oikos_react_engine, "OpenAIChat", FakeOpenAIChat)

    llm = oikos_react_engine._make_llm(model="x-ai/grok-4.1-fast", tools=[], reasoning_effort="none")

    assert isinstance(llm, FakeOpenAIChat)
    assert captured["kwargs"]["model"] == "x-ai/grok-4.1-fast"
    assert captured["kwargs"]["api_key"]  # has a key (real or test)
    assert captured["kwargs"]["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["tools"] == []
    assert captured["tool_choice"] is None


def test_openrouter_reasoning_uses_extra_body_not_reasoning_effort(monkeypatch):
    """OpenRouter models use extra_body.reasoning.effort, not the raw reasoning_effort kwarg."""
    captured: dict[str, object] = {}

    class FakeOpenAIChat:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def bind_tools(self, tools, tool_choice=None):
            return self

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(oikos_react_engine, "OpenAIChat", FakeOpenAIChat)

    oikos_react_engine._make_llm(model="x-ai/grok-4.1-fast", tools=[], reasoning_effort="high")

    assert captured["kwargs"]["model"] == "x-ai/grok-4.1-fast"
    # OpenRouter gets reasoning via extra_body, NOT reasoning_effort kwarg
    assert "reasoning_effort" not in captured["kwargs"], (
        "OpenRouter uses extra_body.reasoning.effort, not reasoning_effort"
    )
    assert captured["kwargs"]["extra_body"] == {"reasoning": {"effort": "high"}}


def test_direct_openai_still_uses_reasoning_effort_kwarg(monkeypatch):
    """Direct OpenAI models should still use the native reasoning_effort parameter."""
    captured: dict[str, object] = {}

    class FakeOpenAIChat:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def bind_tools(self, tools, tool_choice=None):
            return self

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setattr(oikos_react_engine, "OpenAIChat", FakeOpenAIChat)

    oikos_react_engine._make_llm(model="gpt-5.2", tools=[], reasoning_effort="high")

    assert captured["kwargs"]["model"] == "gpt-5.2"
    assert captured["kwargs"]["reasoning_effort"] == "high"
    assert "extra_body" not in captured["kwargs"], (
        "Direct OpenAI should use reasoning_effort, not extra_body"
    )


def test_direct_xai_gets_no_reasoning_param(monkeypatch):
    """Direct xAI models (legacy) should get neither reasoning_effort nor extra_body."""
    captured: dict[str, object] = {}

    class FakeOpenAIChat:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def bind_tools(self, tools, tool_choice=None):
            return self

    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    monkeypatch.setattr(oikos_react_engine, "OpenAIChat", FakeOpenAIChat)

    oikos_react_engine._make_llm(model="grok-4-1-fast-reasoning", tools=[], reasoning_effort="high")

    assert captured["kwargs"]["model"] == "grok-4-1-fast-reasoning"
    assert "reasoning_effort" not in captured["kwargs"]
    assert "extra_body" not in captured["kwargs"]
