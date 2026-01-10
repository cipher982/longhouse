"""Test-only model definitions. Single source of truth.

Test models (gpt-mock, gpt-scripted) are used for deterministic testing:
- gpt-mock: Unit tests - returns canned responses immediately
- gpt-scripted: E2E tests - follows scripted tool call sequences

These models MUST NOT be used in production. The require_testing_mode()
function enforces this constraint at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zerg.config import Settings

# Canonical set of test-only model identifiers
TEST_ONLY_MODELS = frozenset({"gpt-mock", "gpt-scripted"})


def is_test_model(model_id: str) -> bool:
    """Check if model is test-only.

    Args:
        model_id: The model identifier to check

    Returns:
        True if model_id is a test-only model
    """
    return model_id in TEST_ONLY_MODELS


def require_testing_mode(model_id: str, settings: Settings) -> None:
    """Raise if test model used outside testing mode.

    This enforces that test models are only used when TESTING=1.
    Call this before instantiating any LLM with a test model.

    Args:
        model_id: The model identifier being used
        settings: Application settings (must have .testing attribute)

    Raises:
        ValueError: If model_id is a test model and settings.testing is False
    """
    if is_test_model(model_id) and not settings.testing:
        raise ValueError(f"Test model '{model_id}' requires TESTING=1. " "Set environment variable or use a production model.")
