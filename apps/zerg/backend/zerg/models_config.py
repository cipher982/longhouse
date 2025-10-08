from enum import Enum
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
    ):
        self.id = id
        self.display_name = display_name
        self.provider = provider
        self.is_default = is_default

    def to_dict(self) -> Dict:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "is_default": self.is_default,
        }


# Define available models
AVAILABLE_MODELS = [
    ModelConfig(
        id="gpt-4.1-2025-04-14",
        display_name="gpt-4.1",
        provider=ModelProvider.OPENAI,
        is_default=True,
    ),
    ModelConfig(
        id="o3-mini-2025-01-31",
        display_name="o3 mini",
        provider=ModelProvider.OPENAI,
    ),
    ModelConfig(
        id="o1-2024-12-17",
        display_name="o1",
        provider=ModelProvider.OPENAI,
    ),
    # Additional models used in automated tests -------------------------
    ModelConfig(
        id="gpt-4o",
        display_name="gpt-4o",
        provider=ModelProvider.OPENAI,
    ),
    ModelConfig(
        id="gpt-mock",
        display_name="gpt-mock",
        provider=ModelProvider.OPENAI,
    ),
    ModelConfig(
        id="gpt-4o-mini",
        display_name="gpt-4o-mini",
        provider=ModelProvider.OPENAI,
    ),
]

# Create lookup dictionaries for quick access
MODELS_BY_ID: Dict[str, ModelConfig] = {model.id: model for model in AVAILABLE_MODELS}
DEFAULT_MODEL: ModelConfig = next((m for m in AVAILABLE_MODELS if m.is_default), AVAILABLE_MODELS[0])


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
