"""
Centralized model configuration for Zerg.

Loads from shared config/models.json - the single source of truth for all model definitions.

Override the default path via:
  MODELS_CONFIG_PATH=/path/to/models.json

Select per-instance routing overrides via:
  MODELS_PROFILE=oss|david|...
"""

import json
import os
from enum import Enum
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional


class ModelProvider(str, Enum):
    """Enum for different model providers."""

    OPENAI = "openai"
    GROQ = "groq"
    ANTHROPIC = "anthropic"


_PROVIDER_DEFAULT_API_KEY_ENVS = {
    ModelProvider.OPENAI: "OPENAI_API_KEY",
    ModelProvider.GROQ: "GROQ_API_KEY",
    ModelProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
}


class ModelConfig:
    """Simple model configuration."""

    def __init__(
        self,
        id: str,
        display_name: str,
        provider: ModelProvider,
        is_default: bool = False,
        tier: Optional[str] = None,
        description: Optional[str] = None,
        base_url: Optional[str] = None,
        capabilities: Optional[Dict] = None,
        api_key_env_var: Optional[str] = None,
    ):
        self.id = id
        self.display_name = display_name
        self.provider = provider
        self.is_default = is_default
        self.tier = tier
        self.description = description
        self.base_url = base_url
        self.capabilities = capabilities or {}
        self.api_key_env_var = api_key_env_var

    def to_dict(self) -> Dict:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "is_default": self.is_default,
            "capabilities": self.capabilities,
        }


# =============================================================================
# CONFIG LOADING - Direct load at import time (no lazy magic)
# =============================================================================


def _get_config_path() -> Path:
    """Get the models.json config path.

    Priority:
    1. MODELS_CONFIG_PATH env var (explicit override)
    2. Default: relative to this file (works in monorepo and Docker)
    """
    env_path = os.getenv("MODELS_CONFIG_PATH")
    if env_path:
        return Path(env_path)

    # Default: Find config relative to this file
    # Local monorepo: zerg/backend/zerg/models_config.py -> config/models.json
    # Docker: /app/zerg/models_config.py -> /app/../config/models.json
    # Note: .resolve() normalizes paths with .. segments (needed when imported via symlink-like paths)
    return Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "models.json"


def _load_config() -> dict:
    """Load the shared models.json configuration."""
    config_path = _get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Models config not found at {config_path}. "
            f"Set MODELS_CONFIG_PATH env var to override, or ensure config/models.json exists."
        )
    return json.loads(config_path.read_text())


# Load config at import time
_CONFIG = _load_config()
_TEXT_CONFIG = _CONFIG["text"]
_TIERS = _TEXT_CONFIG["tiers"]
_MODELS = _TEXT_CONFIG["models"]


def _resolve_model_reference(tier_or_model: str, *, source: str) -> str:
    """Resolve tier name or direct model ID, and validate it exists."""
    model_id = _TIERS.get(tier_or_model, tier_or_model)
    if model_id not in _MODELS:
        raise ValueError(
            f"Invalid model reference '{tier_or_model}' in {source}. "
            f"Valid tiers: {list(_TIERS.keys())}, valid model IDs: {list(_MODELS.keys())}"
        )
    return model_id


def _build_active_text_routing() -> tuple[str, Dict[str, str], Dict[str, str]]:
    """Resolve active text use-case/default routing with optional profile overrides."""
    base_use_cases = dict(_CONFIG["useCases"]["text"])
    base_defaults = dict(_CONFIG["defaults"]["text"])

    raw_routing_profiles = _CONFIG.get("routingProfiles", {})
    routing_profiles = {name: cfg for name, cfg in raw_routing_profiles.items() if isinstance(cfg, dict) and not name.startswith("$")}
    active_profile = os.getenv("MODELS_PROFILE", "oss")
    if routing_profiles and active_profile not in routing_profiles:
        raise ValueError(f"Unknown MODELS_PROFILE '{active_profile}'. " f"Valid profiles: {list(routing_profiles.keys())}")

    profile_cfg = routing_profiles.get(active_profile, {})
    text_overrides = profile_cfg.get("text", {})
    use_case_overrides = text_overrides.get("useCases", {})
    defaults_overrides = text_overrides.get("defaults", {})

    use_cases = dict(base_use_cases)
    use_cases.update(use_case_overrides)

    defaults = dict(base_defaults)
    defaults.update(defaults_overrides)

    for use_case, tier_or_model in use_cases.items():
        _resolve_model_reference(tier_or_model, source=f"useCases.text.{use_case}")
    for name, tier_or_model in defaults.items():
        _resolve_model_reference(tier_or_model, source=f"defaults.text.{name}")

    return active_profile, use_cases, defaults


_ACTIVE_PROFILE, _USE_CASES, _DEFAULTS = _build_active_text_routing()


def _get_api_key_env_var(model_config: ModelConfig) -> str:
    """Return the env var name for the model's API key."""
    if model_config.api_key_env_var:
        return model_config.api_key_env_var
    return _PROVIDER_DEFAULT_API_KEY_ENVS[model_config.provider]


# =============================================================================
# TIER CONSTANTS - Plain strings (no lazy magic)
# =============================================================================

# Model tiers by capability (change these in config/models.json to update everywhere)
TIER_1: str = _TIERS["TIER_1"]  # Best reasoning (gpt-5.2)
TIER_2: str = _TIERS["TIER_2"]  # Good, cheaper (gpt-5-mini)
TIER_3: str = _TIERS["TIER_3"]  # Basic, cheapest (gpt-5-nano)
# Note: Test models (gpt-mock, gpt-scripted) are defined in zerg.testing.test_models


# =============================================================================
# MODEL CACHE - Built at import time
# =============================================================================

DEFAULT_MODEL_ID: str = _resolve_model_reference(_DEFAULTS["fiche"], source="defaults.text.fiche")
DEFAULT_COMMIS_MODEL_ID: str = _resolve_model_reference(_DEFAULTS["commis"], source="defaults.text.commis")
TEST_MODEL_ID: str = _resolve_model_reference(_DEFAULTS["test"], source="defaults.text.test")

AVAILABLE_MODELS: List[ModelConfig] = []
for _model_id, _model_info in _MODELS.items():
    _provider = ModelProvider(_model_info["provider"])
    _is_default = _model_id == DEFAULT_MODEL_ID
    AVAILABLE_MODELS.append(
        ModelConfig(
            id=_model_id,
            display_name=_model_info["displayName"],
            provider=_provider,
            is_default=_is_default,
            tier=_model_info.get("tier"),
            description=_model_info.get("description"),
            base_url=_model_info.get("baseUrl"),
            capabilities=_model_info.get("capabilities"),
            api_key_env_var=_model_info.get("apiKeyEnvVar"),
        )
    )

MODELS_BY_ID: Dict[str, ModelConfig] = {model.id: model for model in AVAILABLE_MODELS}
DEFAULT_MODEL: ModelConfig = next((m for m in AVAILABLE_MODELS if m.is_default), AVAILABLE_MODELS[0])


# =============================================================================
# USE CASE HELPERS - Get model by what you're doing
# =============================================================================


def get_model_for_use_case(use_case: str) -> str:
    """
    Get the appropriate model ID for a use case.

    Use cases (defined in config/models.json):
    - fiche_conversation: TIER_1 (quality critical)
    - routing_decision: TIER_1 (small output but high-stakes decision)
    - tool_selection: TIER_1 (quality critical)
    - commis_task: TIER_2 (cost-sensitive batch work)
    - summarization: TIER_2 (cost-sensitive by default profile)
    - bulk_classification: TIER_3 (high volume, simple)
    - ci_test: TIER_3 (fast/cheap for CI)

    Values can be tier references (e.g. "TIER_1") or direct model IDs (e.g. "glm-4.7").
    """
    tier_or_model = _USE_CASES.get(use_case)
    if not tier_or_model:
        raise ValueError(f"Unknown use case: {use_case}. Valid: {list(_USE_CASES.keys())}")
    return _resolve_model_reference(tier_or_model, source=f"useCases.text.{use_case}")


def get_active_models_profile() -> str:
    """Return active routing profile from MODELS_PROFILE."""
    return _ACTIVE_PROFILE


def get_api_key_env_var_for_use_case(use_case: str) -> str:
    """Return API key env var required for the use-case's resolved model."""
    model_id = get_model_for_use_case(use_case)
    model_config = MODELS_BY_ID.get(model_id)
    if not model_config:
        raise ValueError(f"Model {model_id} not found in models config")
    return _get_api_key_env_var(model_config)


def validate_use_case_llm_config(use_case: str) -> tuple[str, ModelProvider, str]:
    """Validate that a use case resolves to a model and has required key env var."""
    model_id = get_model_for_use_case(use_case)
    model_config = MODELS_BY_ID.get(model_id)
    if not model_config:
        raise ValueError(f"Model {model_id} not found in models config")

    api_key_env_var = _get_api_key_env_var(model_config)
    if not os.getenv(api_key_env_var):
        raise ValueError(
            f"{api_key_env_var} required for use case '{use_case}' " f"(model='{model_id}', provider='{model_config.provider.value}')"
        )

    return model_id, model_config.provider, api_key_env_var


# =============================================================================
# API FUNCTIONS - For use by routers and services
# =============================================================================


def get_model_by_id(model_id: str) -> Optional[ModelConfig]:
    """Get a model by its ID."""
    return MODELS_BY_ID.get(model_id)


def get_default_model() -> ModelConfig:
    """Get the default model."""
    return DEFAULT_MODEL


def get_default_model_id_str() -> str:
    """Backwards compatibility helper returning default model ID."""
    return DEFAULT_MODEL_ID


def get_default_model_id() -> str:
    """Get the default model ID as a string."""
    return DEFAULT_MODEL.id


def get_all_models() -> List[ModelConfig]:
    """Get all available models."""
    return AVAILABLE_MODELS


def get_all_models_for_api() -> List[Dict]:
    """Get all models in a format suitable for API responses."""
    return [model.to_dict() for model in AVAILABLE_MODELS]


def get_tier_model(tier: str) -> str:
    """
    Get model ID for a tier.

    Args:
        tier: One of "TIER_1", "TIER_2", "TIER_3"

    Returns:
        The model ID for that tier.
    """
    if tier not in _TIERS:
        raise ValueError(f"Unknown tier: {tier}. Valid: {list(_TIERS.keys())}")
    return _TIERS[tier]


# =============================================================================
# LLM CLIENT FACTORY - Get a ready-to-use async client for a use case
# =============================================================================


def get_llm_client_for_use_case(use_case: str) -> tuple:
    """Get an async LLM client + model string for a use case.

    Resolves use case -> tier/model -> provider from models.json (including MODELS_PROFILE
    overrides), then creates the appropriate SDK client.

    API key env var resolution:
      1) model.apiKeyEnvVar (if configured on that model)
      2) provider default env var (OPENAI_API_KEY / GROQ_API_KEY / ANTHROPIC_API_KEY)

    Returns:
        (client, model_id, provider) tuple. Caller must close the client.

    Raises:
        ValueError: If routing or required API key env var is missing.
    """
    model_id, provider, api_key_env_var = validate_use_case_llm_config(use_case)
    model_config = MODELS_BY_ID[model_id]
    api_key = os.getenv(api_key_env_var)
    base_url = model_config.base_url

    if provider == ModelProvider.ANTHROPIC:
        from anthropic import AsyncAnthropic

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return AsyncAnthropic(**kwargs), model_id, provider

    # OpenAI-compatible providers (openai, groq)
    from openai import AsyncOpenAI

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs), model_id, provider
