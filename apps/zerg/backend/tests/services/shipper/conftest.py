"""Shared fixtures for shipper tests."""

import pytest

from zerg.services.shipper.providers import registry as provider_registry


@pytest.fixture(autouse=True)
def _isolate_provider_registry():
    """Prevent non-claude providers from scanning real home directories in tests.

    Codex/Gemini providers auto-register with default config dirs (~/.codex,
    ~/.gemini).  In tests we only want the claude provider (which gets scoped
    to tmp_path by per-test fixtures).
    """
    saved = dict(provider_registry._providers)
    provider_registry._providers = {k: v for k, v in saved.items() if k == "claude"}
    yield
    provider_registry._providers = saved
