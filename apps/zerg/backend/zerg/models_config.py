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
# Voice/realtime interfaces use dedicated realtime models (see jarvis context)
# Note: We use alias IDs without date tags - OpenAI updates snapshots behind the scenes
AVAILABLE_MODELS = [
    ModelConfig(
        id="gpt-5.1",
        display_name="GPT-5.1",
        provider=ModelProvider.OPENAI,
        is_default=True,
    ),
    ModelConfig(
        id="gpt-5-mini",
        display_name="GPT-5 Mini",
        provider=ModelProvider.OPENAI,
    ),
    ModelConfig(
        id="gpt-5-nano",
        display_name="GPT-5 Nano",
        provider=ModelProvider.OPENAI,
    ),
    ModelConfig(
        id="gpt-mock",
        display_name="Mock (testing)",
        provider=ModelProvider.OPENAI,
    ),
]

# Centralized model constants - use these instead of hardcoding
DEFAULT_MODEL_ID = "gpt-5.1"
DEFAULT_WORKER_MODEL_ID = "gpt-5-mini"  # Workers use lighter model by default
TEST_MODEL_ID = "gpt-5-nano"  # For tests that hit real LLM but need to be cheap/fast

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
