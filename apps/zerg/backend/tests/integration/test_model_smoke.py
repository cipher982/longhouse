"""Integration smoke tests for ALL models in config/models.json.

These tests make REAL API calls to verify:
1. Each model can receive a prompt and return a response
2. Each model can bind tools without error
3. Reasoning params are only sent when model supports them

Run with: make test-integration
"""

import os

import pytest
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from zerg.models_config import AVAILABLE_MODELS
from zerg.models_config import ModelProvider


pytestmark = pytest.mark.integration


def has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def has_groq_key() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def get_api_key_for_provider(provider: ModelProvider) -> str | None:
    """Get API key for a provider."""
    if provider == ModelProvider.OPENAI:
        return os.getenv("OPENAI_API_KEY")
    elif provider == ModelProvider.GROQ:
        return os.getenv("GROQ_API_KEY")
    return None


def can_test_model(model) -> bool:
    """Check if we have the API key to test this model."""
    return get_api_key_for_provider(model.provider) is not None


# Collect models by provider for parametrized tests
OPENAI_MODELS = [m for m in AVAILABLE_MODELS if m.provider == ModelProvider.OPENAI]
GROQ_MODELS = [m for m in AVAILABLE_MODELS if m.provider == ModelProvider.GROQ]


@tool
def get_weather(location: str) -> str:
    """Get the weather for a location."""
    return f"Weather in {location}: Sunny, 72°F"


@tool
def add_numbers(a: int, b: int) -> str:
    """Add two numbers together."""
    return f"Result: {a + b}"


TEST_TOOLS = [get_weather, add_numbers]


@pytest.mark.skipif(not has_openai_key(), reason="OPENAI_API_KEY not set")
class TestOpenAIModelSmoke:
    """Smoke tests for all OpenAI models."""

    @pytest.mark.parametrize("model", OPENAI_MODELS, ids=lambda m: m.id)
    def test_model_responds_to_simple_prompt(self, model):
        """Model should return a response to a simple prompt."""
        llm = ChatOpenAI(model=model.id)
        response = llm.invoke("Say 'hello' and nothing else.")

        assert response is not None
        assert response.content is not None
        assert len(response.content) > 0
        # Should contain hello in some form
        assert "hello" in response.content.lower()

    @pytest.mark.parametrize("model", OPENAI_MODELS, ids=lambda m: m.id)
    def test_model_can_bind_tools(self, model):
        """Model should accept tool binding without error."""
        llm = ChatOpenAI(model=model.id)
        llm_with_tools = llm.bind_tools(TEST_TOOLS)

        # Should be able to invoke with tools bound
        response = llm_with_tools.invoke("What's 2 + 2?")
        assert response is not None

    @pytest.mark.parametrize("model", OPENAI_MODELS, ids=lambda m: m.id)
    def test_model_reasoning_param_accepted(self, model):
        """Model should accept reasoning_effort if it supports reasoning."""
        supports_reasoning = model.capabilities.get("reasoning", False)

        if supports_reasoning:
            # Should work with reasoning_effort
            llm = ChatOpenAI(model=model.id, reasoning_effort="low")
            response = llm.invoke("What is 1 + 1?")
            assert response is not None
            assert response.content is not None
        else:
            # Model doesn't support reasoning - should work without param
            llm = ChatOpenAI(model=model.id)
            response = llm.invoke("What is 1 + 1?")
            assert response is not None

    @pytest.mark.parametrize(
        "model",
        [m for m in OPENAI_MODELS if m.capabilities.get("reasoningNone", False)],
        ids=lambda m: m.id,
    )
    def test_model_reasoning_none_accepted(self, model):
        """Models with reasoningNone=true should accept reasoning_effort='none'."""
        llm = ChatOpenAI(model=model.id, reasoning_effort="none")
        response = llm.invoke("What is 1 + 1?")

        assert response is not None
        # Check that reasoning tokens are 0 when none is used
        usage = response.response_metadata.get("token_usage", {})
        completion_details = usage.get("completion_tokens_details", {})
        reasoning_tokens = completion_details.get("reasoning_tokens", 0)
        assert reasoning_tokens == 0, f"Expected 0 reasoning tokens with none, got {reasoning_tokens}"


@pytest.mark.skipif(not has_groq_key(), reason="GROQ_API_KEY not set")
class TestGroqModelSmoke:
    """Smoke tests for all Groq models."""

    @pytest.mark.parametrize("model", GROQ_MODELS, ids=lambda m: m.id)
    def test_model_responds_to_simple_prompt(self, model):
        """Model should return a response to a simple prompt."""
        llm = ChatOpenAI(
            model=model.id,
            base_url=model.base_url,
            api_key=os.getenv("GROQ_API_KEY"),
        )
        response = llm.invoke("Say 'hello' and nothing else.")

        assert response is not None
        assert response.content is not None
        assert len(response.content) > 0

    @pytest.mark.parametrize("model", GROQ_MODELS, ids=lambda m: m.id)
    def test_model_can_bind_tools(self, model):
        """Model should accept tool binding without error."""
        llm = ChatOpenAI(
            model=model.id,
            base_url=model.base_url,
            api_key=os.getenv("GROQ_API_KEY"),
        )
        llm_with_tools = llm.bind_tools(TEST_TOOLS)

        response = llm_with_tools.invoke("What's 2 + 2?")
        assert response is not None

    @pytest.mark.parametrize(
        "model",
        [m for m in GROQ_MODELS if m.capabilities.get("reasoning", False)],
        ids=lambda m: m.id,
    )
    def test_groq_reasoning_model_works(self, model):
        """Groq models with reasoning support should work."""
        # Note: Groq only accepts 'none' or 'default' for reasoning_effort
        llm = ChatOpenAI(
            model=model.id,
            base_url=model.base_url,
            api_key=os.getenv("GROQ_API_KEY"),
            reasoning_effort="default",  # Groq uses 'none' or 'default', not low/medium/high
        )
        response = llm.invoke("What is 2 + 2?")

        assert response is not None
        assert response.content is not None

    @pytest.mark.parametrize(
        "model",
        [m for m in GROQ_MODELS if not m.capabilities.get("reasoning", False)],
        ids=lambda m: m.id,
    )
    def test_groq_non_reasoning_model_works(self, model):
        """Groq models without reasoning should work (no reasoning param)."""
        llm = ChatOpenAI(
            model=model.id,
            base_url=model.base_url,
            api_key=os.getenv("GROQ_API_KEY"),
            # No reasoning_effort param
        )
        response = llm.invoke("What is 2 + 2?")

        assert response is not None
        assert response.content is not None


class TestModelProviderCoverage:
    """Meta-tests to ensure we're testing all providers."""

    def test_all_providers_have_smoke_tests(self):
        """Every provider should have at least one model with smoke tests."""
        providers_in_config = {m.provider for m in AVAILABLE_MODELS}
        providers_with_tests = {ModelProvider.OPENAI, ModelProvider.GROQ}

        missing = providers_in_config - providers_with_tests
        assert not missing, f"Missing smoke tests for providers: {missing}"

    def test_all_models_categorized(self):
        """All models should be in either OPENAI_MODELS or GROQ_MODELS."""
        tested_models = set(m.id for m in OPENAI_MODELS + GROQ_MODELS)
        all_models = set(m.id for m in AVAILABLE_MODELS)

        missing = all_models - tested_models
        assert not missing, f"Models not in any test category: {missing}"


class TestMakeLlmIntegration:
    """Test the _make_llm() function directly to verify config-to-runtime wiring.

    These tests use the same code path as production to catch config wiring issues.
    """

    @pytest.mark.skipif(not has_openai_key(), reason="OPENAI_API_KEY not set")
    @pytest.mark.parametrize("model", OPENAI_MODELS, ids=lambda m: m.id)
    def test_make_llm_openai_models(self, model):
        """_make_llm should correctly configure OpenAI models."""
        from zerg.services.supervisor_react_engine import _make_llm

        llm = _make_llm(model.id, tools=TEST_TOOLS)

        # Verify the LLM was created with correct model
        assert llm is not None

        # Make a real call to verify it works
        response = llm.invoke("Say hello")
        assert response is not None
        assert response.content is not None

    @pytest.mark.skipif(not has_groq_key(), reason="GROQ_API_KEY not set")
    @pytest.mark.parametrize("model", GROQ_MODELS, ids=lambda m: m.id)
    def test_make_llm_groq_models(self, model):
        """_make_llm should correctly configure Groq models with base_url."""
        from zerg.services.supervisor_react_engine import _make_llm

        llm = _make_llm(model.id, tools=TEST_TOOLS)

        # Verify the LLM was created
        assert llm is not None

        # Make a real call to verify it works
        response = llm.invoke("Say hello")
        assert response is not None
        assert response.content is not None

    @pytest.mark.skipif(not has_openai_key(), reason="OPENAI_API_KEY not set")
    def test_make_llm_with_reasoning_effort(self):
        """_make_llm should pass reasoning_effort for capable models."""
        from zerg.services.supervisor_react_engine import _make_llm

        # Get a model that supports reasoning
        reasoning_model = next(
            (m for m in OPENAI_MODELS if m.capabilities.get("reasoning", False)),
            None,
        )
        if not reasoning_model:
            pytest.skip("No OpenAI reasoning model available")

        llm = _make_llm(reasoning_model.id, tools=[], reasoning_effort="medium")

        response = llm.invoke("What is 1 + 1?")
        assert response is not None
        assert response.content is not None
