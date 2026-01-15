"""
Centralized model configuration for Zerg.

Loads from shared config/models.json - the single source of truth for all model definitions.

Override the default path via:
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
    GROQ = "groq"


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
        base_url: Optional[str] = None,
        capabilities: Optional[Dict] = None,
    ):
        self.id = id
        self.display_name = display_name
        self.provider = provider
        self.is_default = is_default
        self.tier = tier
        self.description = description
        self.base_url = base_url
        self.capabilities = capabilities or {}

    def to_dict(self) -> Dict:
        """Convert to dictionary for API responses"""
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
_USE_CASES = _CONFIG["useCases"]["text"]
_DEFAULTS = _CONFIG["defaults"]["text"]


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

DEFAULT_MODEL_ID: str = _TIERS[_DEFAULTS["agent"]]
DEFAULT_WORKER_MODEL_ID: str = _TIERS[_DEFAULTS["worker"]]
TEST_MODEL_ID: str = _TIERS[_DEFAULTS["test"]]

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
    - agent_conversation: TIER_1 (quality critical)
    - routing_decision: TIER_1 (small output but high-stakes decision)
    - tool_selection: TIER_1 (quality critical)
    - worker_task: TIER_2 (cost-sensitive batch work)
    - summarization: TIER_2 (cost-sensitive)
    - bulk_classification: TIER_3 (high volume, simple)
    - ci_test: TIER_3 (fast/cheap for CI)
    """
    tier = _USE_CASES.get(use_case)
    if not tier:
        raise ValueError(f"Unknown use case: {use_case}. Valid: {list(_USE_CASES.keys())}")
    return _TIERS[tier]


# =============================================================================
# BACKWARDS COMPATIBLE ACCESSORS
# =============================================================================


def get_default_model_id_str() -> str:
    """Get the default model ID as a string."""
    return DEFAULT_MODEL_ID


def get_default_worker_model_id_str() -> str:
    """Get the default worker model ID as a string."""
    return DEFAULT_WORKER_MODEL_ID


def get_test_model_id_str() -> str:
    """Get the test model ID as a string."""
    return TEST_MODEL_ID


# =============================================================================
# API FUNCTIONS - For use by routers and services
# =============================================================================


def get_model_by_id(model_id: str) -> Optional[ModelConfig]:
    """Get a model by its ID"""
    return MODELS_BY_ID.get(model_id)


def get_default_model() -> ModelConfig:
    """Get the default model"""
    return DEFAULT_MODEL


def get_default_model_id() -> str:
    """Get the default model ID as a string"""
    return DEFAULT_MODEL.id


def get_all_models() -> List[ModelConfig]:
    """Get all available models"""
    return AVAILABLE_MODELS


def get_all_models_for_api() -> List[Dict]:
    """Get all models in a format suitable for API responses"""
    return [model.to_dict() for model in AVAILABLE_MODELS]


def get_tier_model(tier: str) -> str:
    """
    Get model ID for a tier.

    Args:
        tier: One of "TIER_1", "TIER_2", "TIER_3"

    Returns:
        The model ID for that tier
    """
    if tier not in _TIERS:
        raise ValueError(f"Unknown tier: {tier}. Valid: {list(_TIERS.keys())}")
    return _TIERS[tier]
