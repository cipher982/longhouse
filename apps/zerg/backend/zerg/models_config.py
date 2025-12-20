"""
Centralized model configuration for Zerg.

Loads from shared config/models.json - the single source of truth for all model definitions.

Configuration is loaded lazily on first access. Override the default path via:
  MODELS_CONFIG_PATH=/path/to/models.json
"""

import json
import os
from enum import Enum
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional


class ModelProvider(str, Enum):
    """Enum for different model providers"""

    OPENAI = "openai"


class ModelConfig:
    """Simple model configuration"""

    def __init__(
        self,
        id: str,
        display_name: str,
        provider: ModelProvider,
        is_default: bool = False,
        tier: Optional[str] = None,
        description: Optional[str] = None,
    ):
        self.id = id
        self.display_name = display_name
        self.provider = provider
        self.is_default = is_default
        self.tier = tier
        self.description = description

    def to_dict(self) -> Dict:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "is_default": self.is_default,
        }


# =============================================================================
# LAZY LOADING - Config loaded on first access, not at import time
# =============================================================================

_CONFIG: dict | None = None
_TEXT_CONFIG: dict | None = None
_TIERS: dict | None = None
_MODELS: dict | None = None
_USE_CASES: dict | None = None
_DEFAULTS: dict | None = None


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
    return Path(__file__).parent.parent.parent.parent.parent / "config" / "models.json"


def _load_models_config() -> dict:
    """Load the shared models.json configuration (lazy, called on first access)."""
    config_path = _get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Models config not found at {config_path}. "
            f"Set MODELS_CONFIG_PATH env var to override, or ensure config/models.json exists."
        )
    with open(config_path) as f:
        return json.load(f)


def _ensure_loaded() -> None:
    """Ensure configuration is loaded (lazy initialization)."""
    global _CONFIG, _TEXT_CONFIG, _TIERS, _MODELS, _USE_CASES, _DEFAULTS
    if _CONFIG is not None:
        return  # Already loaded

    _CONFIG = _load_models_config()
    _TEXT_CONFIG = _CONFIG["text"]
    _TIERS = _TEXT_CONFIG["tiers"]
    _MODELS = _TEXT_CONFIG["models"]
    _USE_CASES = _CONFIG["useCases"]["text"]
    _DEFAULTS = _CONFIG["defaults"]["text"]


# =============================================================================
# TIER ACCESSORS - Use these for semantic model selection (lazy-loaded)
# =============================================================================


def _get_tier(tier_name: str) -> str:
    """Get tier model ID with lazy loading."""
    _ensure_loaded()
    return _TIERS[tier_name]  # type: ignore[index]


def _get_mock_model() -> str:
    """Get mock model ID with lazy loading."""
    _ensure_loaded()
    return _TEXT_CONFIG["mock"]  # type: ignore[index]


# Tier properties as module-level callables for backwards compatibility
# These are evaluated on access, not at import time
class _LazyTier:
    """Lazy accessor for tier constants."""

    def __init__(self, tier_name: str):
        self._tier_name = tier_name

    def __str__(self) -> str:
        return _get_tier(self._tier_name)

    def __repr__(self) -> str:
        return f"_LazyTier({self._tier_name!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return str(self) == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(str(self))


class _LazyMock:
    """Lazy accessor for mock model constant."""

    def __str__(self) -> str:
        return _get_mock_model()

    def __repr__(self) -> str:
        return "_LazyMock()"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return str(self) == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(str(self))


# Model tiers by capability (change these in config/models.json to update everywhere)
# These are lazy - config is loaded on first string conversion/comparison
TIER_1 = _LazyTier("TIER_1")  # Best reasoning (gpt-5.1)
TIER_2 = _LazyTier("TIER_2")  # Good, cheaper (gpt-5-mini)
TIER_3 = _LazyTier("TIER_3")  # Basic, cheapest (gpt-5-nano)
MOCK_MODEL = _LazyMock()  # For unit tests

# =============================================================================
# USE CASE HELPERS - Get model by what you're doing
# =============================================================================


def get_model_for_use_case(use_case: str) -> str:
    """
    Get the appropriate model ID for a use case.

    Use cases (defined in config/models.json):
    - agent_conversation: TIER_1 (quality critical)
    - routing_decision: TIER_1 (small output but high-stakes decision)
    - tool_selection: TIER_1 (quality critical)
    - worker_task: TIER_2 (cost-sensitive batch work)
    - summarization: TIER_2 (cost-sensitive)
    - bulk_classification: TIER_3 (high volume, simple)
    - ci_test: TIER_3 (fast/cheap for CI)
    """
    _ensure_loaded()
    tier = _USE_CASES.get(use_case)  # type: ignore[union-attr]
    if not tier:
        raise ValueError(f"Unknown use case: {use_case}. Valid: {list(_USE_CASES.keys())}")  # type: ignore[arg-type]
    return _TIERS[tier]  # type: ignore[index]


# =============================================================================
# BACKWARDS COMPATIBLE CONSTANTS - Lazy loaded
# =============================================================================

# Lazy accessors for backward-compatible constants
_AVAILABLE_MODELS: List[ModelConfig] | None = None
_MODELS_BY_ID: Dict[str, ModelConfig] | None = None
_DEFAULT_MODEL: ModelConfig | None = None
_DEFAULT_MODEL_ID: str | None = None
_DEFAULT_WORKER_MODEL_ID: str | None = None
_TEST_MODEL_ID: str | None = None


def _build_models_cache() -> None:
    """Build model caches on first access."""
    global _AVAILABLE_MODELS, _MODELS_BY_ID, _DEFAULT_MODEL
    global _DEFAULT_MODEL_ID, _DEFAULT_WORKER_MODEL_ID, _TEST_MODEL_ID

    if _AVAILABLE_MODELS is not None:
        return  # Already built

    _ensure_loaded()

    _DEFAULT_MODEL_ID = _TIERS[_DEFAULTS["agent"]]  # type: ignore[index]
    _DEFAULT_WORKER_MODEL_ID = _TIERS[_DEFAULTS["worker"]]  # type: ignore[index]
    _TEST_MODEL_ID = _TIERS[_DEFAULTS["test"]]  # type: ignore[index]

    _AVAILABLE_MODELS = []
    for model_id, model_info in _MODELS.items():  # type: ignore[union-attr]
        provider = ModelProvider(model_info["provider"])
        is_default = model_id == _DEFAULT_MODEL_ID
        _AVAILABLE_MODELS.append(
            ModelConfig(
                id=model_id,
                display_name=model_info["displayName"],
                provider=provider,
                is_default=is_default,
                tier=model_info.get("tier"),
                description=model_info.get("description"),
            )
        )

    _MODELS_BY_ID = {model.id: model for model in _AVAILABLE_MODELS}
    _DEFAULT_MODEL = next((m for m in _AVAILABLE_MODELS if m.is_default), _AVAILABLE_MODELS[0])


# Public accessors for backwards compatibility
def get_default_model_id_str() -> str:
    """Get the default model ID as a string."""
    _build_models_cache()
    return _DEFAULT_MODEL_ID  # type: ignore[return-value]


def get_default_worker_model_id_str() -> str:
    """Get the default worker model ID as a string."""
    _build_models_cache()
    return _DEFAULT_WORKER_MODEL_ID  # type: ignore[return-value]


def get_test_model_id_str() -> str:
    """Get the test model ID as a string."""
    _build_models_cache()
    return _TEST_MODEL_ID  # type: ignore[return-value]


# Backwards-compatible module-level "constants" via __getattr__
# These trigger lazy loading when accessed
def __getattr__(name: str):
    """Lazy loading for module-level constants."""
    if name == "DEFAULT_MODEL_ID":
        _build_models_cache()
        return _DEFAULT_MODEL_ID
    elif name == "DEFAULT_WORKER_MODEL_ID":
        _build_models_cache()
        return _DEFAULT_WORKER_MODEL_ID
    elif name == "TEST_MODEL_ID":
        _build_models_cache()
        return _TEST_MODEL_ID
    elif name == "AVAILABLE_MODELS":
        _build_models_cache()
        return _AVAILABLE_MODELS
    elif name == "MODELS_BY_ID":
        _build_models_cache()
        return _MODELS_BY_ID
    elif name == "DEFAULT_MODEL":
        _build_models_cache()
        return _DEFAULT_MODEL
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# =============================================================================
# API FUNCTIONS - For use by routers and services
# =============================================================================


def get_model_by_id(model_id: str) -> Optional[ModelConfig]:
    """Get a model by its ID"""
    _build_models_cache()
    return _MODELS_BY_ID.get(model_id)  # type: ignore[union-attr]


def get_default_model() -> ModelConfig:
    """Get the default model"""
    _build_models_cache()
    return _DEFAULT_MODEL  # type: ignore[return-value]


def get_default_model_id() -> str:
    """Get the default model ID as a string"""
    _build_models_cache()
    return _DEFAULT_MODEL.id  # type: ignore[union-attr]


def get_all_models() -> List[ModelConfig]:
    """Get all available models"""
    _build_models_cache()
    return _AVAILABLE_MODELS  # type: ignore[return-value]


def get_all_models_for_api() -> List[Dict]:
    """Get all models in a format suitable for API responses"""
    _build_models_cache()
    return [model.to_dict() for model in _AVAILABLE_MODELS]  # type: ignore[union-attr]


def get_tier_model(tier: str) -> str:
    """
    Get model ID for a tier.

    Args:
        tier: One of "TIER_1", "TIER_2", "TIER_3"

    Returns:
        The model ID for that tier
    """
    _ensure_loaded()
    if tier not in _TIERS:  # type: ignore[operator]
        raise ValueError(f"Unknown tier: {tier}. Valid: {list(_TIERS.keys())}")  # type: ignore[arg-type]
    return _TIERS[tier]  # type: ignore[index]
