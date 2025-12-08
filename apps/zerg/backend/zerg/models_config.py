"""
Centralized model configuration for Zerg.

Loads from shared config/models.json - the single source of truth for all model definitions.
"""

import json
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


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


def _load_models_config() -> dict:
    """Load the shared models.json configuration"""
    # Find config relative to this file: zerg/backend/zerg/models_config.py -> config/models.json
    config_path = Path(__file__).parent.parent.parent.parent.parent / "config" / "models.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Models config not found at {config_path}")
    with open(config_path) as f:
        return json.load(f)


# Load configuration at module level
_CONFIG = _load_models_config()
_TEXT_CONFIG = _CONFIG["text"]
_TIERS = _TEXT_CONFIG["tiers"]
_MODELS = _TEXT_CONFIG["models"]
_USE_CASES = _CONFIG["useCases"]["text"]
_DEFAULTS = _CONFIG["defaults"]["text"]

# =============================================================================
# TIER CONSTANTS - Use these for semantic model selection
# =============================================================================

# Model tiers by capability (change these in config/models.json to update everywhere)
TIER_1 = _TIERS["TIER_1"]  # Best reasoning (gpt-5.1)
TIER_2 = _TIERS["TIER_2"]  # Good, cheaper (gpt-5-mini)
TIER_3 = _TIERS["TIER_3"]  # Basic, cheapest (gpt-5-nano)
MOCK_MODEL = _TEXT_CONFIG["mock"]  # For unit tests

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
# BACKWARDS COMPATIBLE CONSTANTS - Existing code uses these
# =============================================================================

# Default model IDs (backwards compatible)
DEFAULT_MODEL_ID = _TIERS[_DEFAULTS["agent"]]  # "gpt-5.1"
DEFAULT_WORKER_MODEL_ID = _TIERS[_DEFAULTS["worker"]]  # "gpt-5-mini"
TEST_MODEL_ID = _TIERS[_DEFAULTS["test"]]  # "gpt-5-nano"

# Build AVAILABLE_MODELS list from config
AVAILABLE_MODELS: List[ModelConfig] = []
for model_id, model_info in _MODELS.items():
    provider = ModelProvider(model_info["provider"])
    is_default = model_id == DEFAULT_MODEL_ID
    AVAILABLE_MODELS.append(
        ModelConfig(
            id=model_id,
            display_name=model_info["displayName"],
            provider=provider,
            is_default=is_default,
            tier=model_info.get("tier"),
            description=model_info.get("description"),
        )
    )

# Create lookup dictionaries for quick access
MODELS_BY_ID: Dict[str, ModelConfig] = {model.id: model for model in AVAILABLE_MODELS}
DEFAULT_MODEL: ModelConfig = next((m for m in AVAILABLE_MODELS if m.is_default), AVAILABLE_MODELS[0])

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
