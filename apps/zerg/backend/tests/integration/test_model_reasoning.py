"""Integration tests for model reasoning capabilities.

These tests make REAL API calls to validate:
1. OpenAI models accept reasoning_effort parameter when supported
2. Groq models with reasoning capability work correctly
3. Non-reasoning models work without reasoning param

Run with: make test-integration
Skip in CI: these tests require API keys and cost money.
"""

import os

import pytest
from langchain_openai import ChatOpenAI

from zerg.models_config import AVAILABLE_MODELS
from zerg.models_config import ModelProvider


# Skip all tests if no API keys
pytestmark = pytest.mark.integration


def has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def has_groq_key() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


# Collect models by provider and capabilities from config
OPENAI_MODELS = [m for m in AVAILABLE_MODELS if m.provider == ModelProvider.OPENAI]
GROQ_MODELS = [m for m in AVAILABLE_MODELS if m.provider == ModelProvider.GROQ]

OPENAI_REASONING_MODELS = [m for m in OPENAI_MODELS if m.capabilities.get("reasoning", False)]
OPENAI_REASONING_NONE_MODELS = [m for m in OPENAI_MODELS if m.capabilities.get("reasoningNone", False)]
GROQ_REASONING_MODELS = [m for m in GROQ_MODELS if m.capabilities.get("reasoning", False)]
GROQ_NON_REASONING_MODELS = [m for m in GROQ_MODELS if not m.capabilities.get("reasoning", False)]


@pytest.mark.skipif(not has_openai_key(), reason="OPENAI_API_KEY not set")
class TestOpenAIReasoning:
    """Test OpenAI model reasoning capabilities."""

    @pytest.mark.parametrize("model", OPENAI_REASONING_MODELS, ids=lambda m: m.id)
    def test_reasoning_model_accepts_medium(self, model):
        """Models with reasoning=true should accept reasoning_effort=medium."""
        llm = ChatOpenAI(
            model=model.id,
            reasoning_effort="medium",
        )

        response = llm.invoke("What is 2 + 2?")

        assert response is not None
        assert response.content is not None
        assert len(response.content) > 0

    @pytest.mark.parametrize("model", OPENAI_REASONING_NONE_MODELS, ids=lambda m: m.id)
    def test_reasoning_none_model_accepts_none(self, model):
        """Models with reasoningNone=true should accept reasoning_effort=none."""
        llm = ChatOpenAI(
            model=model.id,
            reasoning_effort="none",
        )

        response = llm.invoke("What is 2 + 2?")

        assert response is not None
        # Check reasoning tokens are 0 when none is used
        usage = response.response_metadata.get("token_usage", {})
        completion_details = usage.get("completion_tokens_details", {})
        reasoning_tokens = completion_details.get("reasoning_tokens", 0)
        assert reasoning_tokens == 0, f"Expected 0 reasoning tokens with none, got {reasoning_tokens}"


@pytest.mark.skipif(not has_groq_key(), reason="GROQ_API_KEY not set")
class TestGroqReasoning:
    """Test Groq model reasoning capabilities."""

    @pytest.mark.parametrize("model", GROQ_REASONING_MODELS, ids=lambda m: m.id)
    def test_groq_reasoning_model_works(self, model):
        """Groq models with reasoning=true should work with reasoning_effort=default."""
        # Note: Groq only accepts 'none' or 'default' for reasoning_effort
        llm = ChatOpenAI(
            model=model.id,
            base_url=model.base_url,
            api_key=os.getenv("GROQ_API_KEY"),
            reasoning_effort="default",
        )

        response = llm.invoke("What is 2 + 2?")

        assert response.content is not None
        assert len(response.content) > 0

    @pytest.mark.parametrize("model", GROQ_NON_REASONING_MODELS, ids=lambda m: m.id)
    def test_groq_non_reasoning_model_works(self, model):
        """Groq models without reasoning should work without reasoning param."""
        llm = ChatOpenAI(
            model=model.id,
            base_url=model.base_url,
            api_key=os.getenv("GROQ_API_KEY"),
            # Don't pass reasoning_effort - model doesn't support it
        )

        response = llm.invoke("What is 2 + 2?")

        assert response.content is not None
        assert len(response.content) > 0


class TestModelValidation:
    """Test model validation in _make_llm.

    Note: These tests don't require API keys - they test validation logic only.
    """

    def test_unknown_model_raises_error(self):
        """Unknown model ID should raise ValueError."""
        from zerg.services.concierge_react_engine import _make_llm

        with pytest.raises(ValueError, match="Unknown model"):
            _make_llm("nonexistent-model-xyz", tools=[])

    def test_groq_model_without_api_key_raises_error(self):
        """Groq model without GROQ_API_KEY should raise ValueError."""
        from unittest.mock import MagicMock, patch

        from zerg.services.concierge_react_engine import _make_llm

        # Mock get_settings to return settings with no groq_api_key
        mock_settings = MagicMock()
        mock_settings.groq_api_key = None
        mock_settings.openai_api_key = "test-key"

        with patch("zerg.config.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError, match="GROQ_API_KEY not configured"):
                _make_llm("qwen/qwen3-32b", tools=[])
