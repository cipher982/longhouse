"""Validation tests for Gmail Pub/Sub configuration."""

from __future__ import annotations

from zerg.config import _validate_required
from zerg.config import get_settings


def test_validate_required_requires_pubsub_audience_when_gmail_topic_is_configured():
    settings = get_settings()
    settings.testing = False
    settings.demo_mode = False
    settings.auth_disabled = True
    settings.database_url = "sqlite:///tmp/test.db"
    settings.fernet_secret = "test-fernet-secret"
    settings.gmail_pubsub_topic = "projects/demo/topics/gmail"
    settings.pubsub_audience = None

    try:
        _validate_required(settings)
    except RuntimeError as exc:
        assert "PUBSUB_AUDIENCE" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("Expected PUBSUB_AUDIENCE validation failure")
