"""Test gmail_sync job conditional registration based on LIFE_HUB_API_KEY."""

import importlib
import os

import pytest


class TestGmailSyncConditionalRegistration:
    """Test that gmail-sync job only registers when LIFE_HUB_API_KEY is set."""

    def test_gmail_sync_not_registered_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Job should not register when LIFE_HUB_API_KEY is not set."""
        # Ensure no API key is set
        monkeypatch.delenv("LIFE_HUB_API_KEY", raising=False)

        # Clear and reimport to trigger registration logic
        from zerg.jobs import registry

        registry.job_registry._jobs.pop("gmail-sync", None)

        # Reimport the module to re-run registration
        import zerg.jobs.life_hub.gmail_sync as gmail_module

        importlib.reload(gmail_module)

        # Job should not be registered
        assert "gmail-sync" not in registry.job_registry._jobs

    def test_gmail_sync_registered_with_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Job should register when LIFE_HUB_API_KEY is set."""
        # Set API key
        monkeypatch.setenv("LIFE_HUB_API_KEY", "test-api-key")

        # Clear and reimport to trigger registration logic
        from zerg.jobs import registry

        registry.job_registry._jobs.pop("gmail-sync", None)

        # Reimport the module to re-run registration
        import zerg.jobs.life_hub.gmail_sync as gmail_module

        importlib.reload(gmail_module)

        # Job should be registered
        assert "gmail-sync" in registry.job_registry._jobs

        # Cleanup
        registry.job_registry._jobs.pop("gmail-sync", None)
