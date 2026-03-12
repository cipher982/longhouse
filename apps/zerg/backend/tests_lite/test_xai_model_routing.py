from __future__ import annotations

import importlib
import os

import zerg.models_config as models_config
from zerg.services import oikos_react_engine


def _reload_models_config() -> None:
    importlib.reload(models_config)


def test_hosted_profile_summary_update_routes_to_xai(monkeypatch):
    original_profile = os.environ.get("MODELS_PROFILE")
    original_xai_key = os.environ.get("XAI_API_KEY")

    monkeypatch.setenv("MODELS_PROFILE", "hosted")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    _reload_models_config()

    try:
        model_id, provider, key_env = models_config.validate_use_case_llm_config("summary_update")
        assert model_id == "grok-4-1-fast-reasoning"
        assert provider == models_config.ModelProvider.OPENAI
        assert key_env == "XAI_API_KEY"
    finally:
        if original_profile is None:
            monkeypatch.delenv("MODELS_PROFILE", raising=False)
        else:
            monkeypatch.setenv("MODELS_PROFILE", original_profile)
        if original_xai_key is None:
            monkeypatch.delenv("XAI_API_KEY", raising=False)
        else:
            monkeypatch.setenv("XAI_API_KEY", original_xai_key)
        _reload_models_config()


def test_oikos_make_llm_uses_xai_api_key_and_base_url(monkeypatch):
    captured: dict[str, object] = {}

    class FakeOpenAIChat:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def bind_tools(self, tools, tool_choice=None):
            captured["tools"] = tools
            captured["tool_choice"] = tool_choice
            return self

    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    monkeypatch.setattr(oikos_react_engine, "OpenAIChat", FakeOpenAIChat)

    llm = oikos_react_engine._make_llm(model="grok-4-1-fast-reasoning", tools=[], reasoning_effort="none")

    assert isinstance(llm, FakeOpenAIChat)
    assert captured["kwargs"]["model"] == "grok-4-1-fast-reasoning"
    assert captured["kwargs"]["api_key"] == "xai-test-key"
    assert captured["kwargs"]["base_url"] == "https://api.x.ai/v1"
    assert "reasoning_effort" not in captured["kwargs"]
    assert captured["tools"] == []
    assert captured["tool_choice"] is None
