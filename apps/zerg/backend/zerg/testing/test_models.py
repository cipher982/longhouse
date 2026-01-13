"""Test-only model definitions. Single source of truth.

Test models (gpt-mock, gpt-scripted) are used for deterministic testing:
- gpt-mock: Unit tests - returns canned responses immediately
- gpt-scripted: E2E tests - follows scripted tool call sequences

These models are allowed in production to support E2E testing against
live environments. The safety mechanism is that they are not selectable
in the UI.
"""

from __future__ import annotations

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


def warn_if_test_model(model_id: str) -> None:
    """Log a warning if a test model is used in active runtime.

    Test models (gpt-mock, gpt-scripted) are allowed in production
    to support E2E testing against live environments. The safety
    mechanism is that these models are not selectable in the UI.

    Args:
        model_id: The model identifier being used
    """
    if is_test_model(model_id):
        import logging

        logging.getLogger(__name__).warning(f"Using test-only model '{model_id}' in active runtime.")
