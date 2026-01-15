"""Smoke tests for model configuration.

Unit tests that validate ALL models in config/models.json:
1. All required fields are present
2. Capabilities schema is valid
3. Provider enum is valid
4. Tier references are valid

Run with: make test (included in regular test suite)
"""

import pytest

from zerg.models_config import AVAILABLE_MODELS
from zerg.models_config import ModelProvider
from zerg.models_config import _CONFIG
from zerg.models_config import _MODELS
from zerg.models_config import _TIERS
from zerg.models_config import get_model_by_id


class TestModelConfigSchema:
    """Validate models.json schema structure."""

    def test_all_models_have_required_fields(self):
        """Every model must have displayName, provider."""
        for model_id, model_info in _MODELS.items():
            assert "displayName" in model_info, f"{model_id} missing displayName"
            assert "provider" in model_info, f"{model_id} missing provider"

    def test_all_models_have_capabilities(self):
        """Every text model must have a capabilities object."""
        for model_id, model_info in _MODELS.items():
            assert "capabilities" in model_info, f"{model_id} missing capabilities"
            assert isinstance(model_info["capabilities"], dict), f"{model_id} capabilities must be dict"

    def test_capabilities_have_reasoning_field(self):
        """Every model's capabilities must specify reasoning support."""
        for model_id, model_info in _MODELS.items():
            caps = model_info.get("capabilities", {})
            assert "reasoning" in caps, f"{model_id} capabilities missing 'reasoning' field"
            assert isinstance(caps["reasoning"], bool), f"{model_id} reasoning must be bool"

    def test_reasoning_none_consistent(self):
        """If reasoning=false, reasoningNone should not be true."""
        for model_id, model_info in _MODELS.items():
            caps = model_info.get("capabilities", {})
            if not caps.get("reasoning", False):
                # If model doesn't support reasoning, reasoningNone should be false or absent
                reasoning_none = caps.get("reasoningNone", False)
                assert not reasoning_none, (
                    f"{model_id} has reasoning=false but reasoningNone=true (inconsistent)"
                )

    def test_all_providers_are_valid_enum(self):
        """All provider values must be valid ModelProvider enum members."""
        valid_providers = {p.value for p in ModelProvider}
        for model_id, model_info in _MODELS.items():
            provider = model_info["provider"]
            assert provider in valid_providers, (
                f"{model_id} has invalid provider '{provider}'. Valid: {valid_providers}"
            )

    def test_groq_models_have_base_url(self):
        """Groq models should specify baseUrl."""
        for model_id, model_info in _MODELS.items():
            if model_info["provider"] == "groq":
                assert "baseUrl" in model_info, f"Groq model {model_id} missing baseUrl"
                assert "groq.com" in model_info["baseUrl"], f"{model_id} baseUrl doesn't look like Groq"

    def test_tier_references_are_valid(self):
        """All tier references in models must exist in tiers config."""
        valid_tiers = set(_TIERS.keys())
        for model_id, model_info in _MODELS.items():
            if "tier" in model_info:
                tier = model_info["tier"]
                assert tier in valid_tiers, f"{model_id} references invalid tier '{tier}'"

    def test_tier_models_exist(self):
        """All models referenced in tiers must exist."""
        for tier_name, model_id in _TIERS.items():
            assert model_id in _MODELS, f"Tier {tier_name} references non-existent model '{model_id}'"


class TestModelConfigLoading:
    """Validate model config loading into Python objects."""

    def test_all_models_loaded(self):
        """All models from JSON should be in AVAILABLE_MODELS."""
        loaded_ids = {m.id for m in AVAILABLE_MODELS}
        config_ids = set(_MODELS.keys())
        assert loaded_ids == config_ids, f"Mismatch: loaded={loaded_ids}, config={config_ids}"

    def test_model_by_id_returns_correct_model(self):
        """get_model_by_id should return the correct model."""
        for model_id in _MODELS.keys():
            model = get_model_by_id(model_id)
            assert model is not None, f"get_model_by_id('{model_id}') returned None"
            assert model.id == model_id

    def test_model_capabilities_loaded(self):
        """Capabilities should be accessible on ModelConfig objects."""
        for model in AVAILABLE_MODELS:
            assert model.capabilities is not None, f"{model.id} has None capabilities"
            assert isinstance(model.capabilities, dict), f"{model.id} capabilities not dict"

    def test_no_duplicate_model_ids(self):
        """Model IDs should be unique."""
        ids = [m.id for m in AVAILABLE_MODELS]
        assert len(ids) == len(set(ids)), f"Duplicate model IDs found: {ids}"


class TestRealtimeModels:
    """Validate realtime model configuration."""

    def test_realtime_models_exist(self):
        """Realtime models section should exist and have models."""
        assert "realtime" in _CONFIG, "Missing realtime section in config"
        assert "models" in _CONFIG["realtime"], "Missing models in realtime section"
        assert len(_CONFIG["realtime"]["models"]) > 0, "No realtime models defined"

    def test_realtime_models_have_required_fields(self):
        """Realtime models should have displayName and provider."""
        realtime_models = _CONFIG["realtime"]["models"]
        for model_id, model_info in realtime_models.items():
            assert "displayName" in model_info, f"Realtime {model_id} missing displayName"
            assert "provider" in model_info, f"Realtime {model_id} missing provider"

    def test_realtime_tiers_exist(self):
        """Realtime tiers should be defined."""
        assert "tiers" in _CONFIG["realtime"], "Missing tiers in realtime section"
        assert len(_CONFIG["realtime"]["tiers"]) > 0, "No realtime tiers defined"

    def test_realtime_tier_models_exist(self):
        """All models referenced in realtime tiers must exist."""
        realtime_tiers = _CONFIG["realtime"]["tiers"]
        realtime_models = _CONFIG["realtime"]["models"]
        for tier_name, model_id in realtime_tiers.items():
            assert model_id in realtime_models, (
                f"Realtime tier {tier_name} references non-existent model '{model_id}'"
            )

    def test_realtime_aliases_reference_valid_models(self):
        """Realtime aliases should reference existing models."""
        if "aliases" not in _CONFIG["realtime"]:
            pytest.skip("No aliases defined in realtime config")
        realtime_aliases = _CONFIG["realtime"]["aliases"]
        realtime_models = _CONFIG["realtime"]["models"]
        for alias, model_id in realtime_aliases.items():
            assert model_id in realtime_models, (
                f"Realtime alias '{alias}' references non-existent model '{model_id}'"
            )

    def test_realtime_defaults_reference_valid_tiers(self):
        """Realtime defaults should reference valid tiers."""
        realtime_defaults = _CONFIG["defaults"].get("realtime", {})
        realtime_tiers = set(_CONFIG["realtime"]["tiers"].keys())
        for context, tier in realtime_defaults.items():
            assert tier in realtime_tiers, (
                f"Realtime default '{context}' references invalid tier '{tier}'"
            )

    def test_realtime_use_cases_reference_valid_tiers(self):
        """Realtime use cases should reference valid tiers."""
        realtime_use_cases = _CONFIG["useCases"].get("realtime", {})
        realtime_tiers = set(_CONFIG["realtime"]["tiers"].keys())
        for use_case, tier in realtime_use_cases.items():
            assert tier in realtime_tiers, (
                f"Realtime use case '{use_case}' references invalid tier '{tier}'"
            )


class TestUseCaseMapping:
    """Validate use case to tier mappings."""

    def test_all_use_cases_map_to_valid_tiers(self):
        """Use case mappings should reference valid tiers."""
        use_cases = _CONFIG["useCases"]["text"]
        valid_tiers = set(_TIERS.keys())
        for use_case, tier in use_cases.items():
            assert tier in valid_tiers, f"Use case '{use_case}' maps to invalid tier '{tier}'"

    def test_default_tiers_are_valid(self):
        """Default tier mappings should be valid."""
        defaults = _CONFIG["defaults"]["text"]
        valid_tiers = set(_TIERS.keys())
        for context, tier in defaults.items():
            assert tier in valid_tiers, f"Default '{context}' maps to invalid tier '{tier}'"
