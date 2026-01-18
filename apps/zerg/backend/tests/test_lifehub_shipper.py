import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from zerg.services.lifehub_shipper import (
    _redact_sensitive_fields,
    _shipped_run_ids,
    ship_run_to_lifehub,
)


@dataclass
class _FakeEvent:
    event_type: str
    payload: dict
    created_at: datetime


@contextmanager
def _fake_db_session():
    yield object()


def _settings(**overrides):
    data = {
        "testing": False,
        "lifehub_shipping_enabled": True,
        "lifehub_api_key": "test-key",
        "lifehub_url": "https://data.example",
        "environment": "development",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture(autouse=True)
def _clear_shipped_ids():
    """Clear the shipped run IDs set before each test."""
    _shipped_run_ids.clear()
    yield
    _shipped_run_ids.clear()


@pytest.mark.asyncio
async def test_ship_run_skips_when_disabled():
    with (
        patch("zerg.services.lifehub_shipper.get_settings", return_value=_settings(lifehub_shipping_enabled=False)),
        patch("zerg.services.lifehub_shipper.EventStore.get_events_after") as mock_get,
        patch("zerg.services.lifehub_shipper.httpx.AsyncClient") as mock_client,
    ):
        await ship_run_to_lifehub(123, "trace-xyz")

        mock_get.assert_not_called()
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_ship_run_skips_when_url_empty():
    """Shipping should be skipped when LIFE_HUB_URL is empty or not configured."""
    with (
        patch("zerg.services.lifehub_shipper.get_settings", return_value=_settings(lifehub_url="")),
        patch("zerg.services.lifehub_shipper.EventStore.get_events_after") as mock_get,
        patch("zerg.services.lifehub_shipper.httpx.AsyncClient") as mock_client,
    ):
        await ship_run_to_lifehub(123, "trace-xyz")

        mock_get.assert_not_called()
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_ship_run_works_without_api_key():
    """Shipping should work without API key (it's optional)."""
    now = datetime(2026, 1, 18, 0, 0, 0, tzinfo=timezone.utc)
    events = [_FakeEvent("supervisor_started", {"task": "hi"}, now)]

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))

    with (
        patch("zerg.services.lifehub_shipper.get_settings", return_value=_settings(lifehub_api_key=None)),
        patch("zerg.services.lifehub_shipper.db_session", _fake_db_session),
        patch("zerg.services.lifehub_shipper.EventStore.get_events_after", return_value=events),
        patch("zerg.services.lifehub_shipper.httpx.AsyncClient", return_value=mock_client),
    ):
        await ship_run_to_lifehub(999, "trace-xyz")

    assert mock_client.post.called
    _, kwargs = mock_client.post.call_args
    # No X-API-Key header when api_key is None
    assert "X-API-Key" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_ship_run_posts_filtered_events():
    now = datetime(2026, 1, 18, 0, 0, 0, tzinfo=timezone.utc)
    events = [
        _FakeEvent("supervisor_started", {"task": "hi"}, now),
        _FakeEvent("supervisor_token", {"token": "x"}, now),
        _FakeEvent("supervisor_tool_completed", {"tool_name": "search"}, now),
        _FakeEvent("run_updated", {"status": "success"}, now),
        _FakeEvent("worker_complete", {"status": "success"}, now),
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))

    with (
        patch("zerg.services.lifehub_shipper.get_settings", return_value=_settings()),
        patch("zerg.services.lifehub_shipper.db_session", _fake_db_session),
        patch("zerg.services.lifehub_shipper.EventStore.get_events_after", return_value=events),
        patch("zerg.services.lifehub_shipper.httpx.AsyncClient", return_value=mock_client),
    ):
        await ship_run_to_lifehub(42, "trace-abc")

    assert mock_client.post.called
    _, kwargs = mock_client.post.call_args
    assert kwargs["headers"]["X-API-Key"] == "test-key"
    assert kwargs["json"]["provider"] == "swarmlet"
    assert kwargs["json"]["provider_session_id"] == "trace-abc"
    assert kwargs["json"]["source_path"] == "zerg://runs/42"
    # Development environment should use "zerg-dev" device_id
    assert kwargs["json"]["device_id"] == "zerg-dev"

    shipped_events = kwargs["json"]["events"]
    # supervisor_token + run_updated should be filtered out
    assert len(shipped_events) == 3

    first = json.loads(shipped_events[0]["raw_text"])
    assert first["event_type"] == "supervisor_started"
    assert first["payload"] == {"task": "hi"}
    assert shipped_events[0]["timestamp"] == now.isoformat()


@pytest.mark.asyncio
async def test_ship_run_uses_production_device_id():
    """Verify device_id is 'zerg-prod' for production environment."""
    now = datetime(2026, 1, 18, 0, 0, 0, tzinfo=timezone.utc)
    events = [_FakeEvent("supervisor_started", {"task": "hi"}, now)]

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))

    with (
        patch("zerg.services.lifehub_shipper.get_settings", return_value=_settings(environment="production")),
        patch("zerg.services.lifehub_shipper.db_session", _fake_db_session),
        patch("zerg.services.lifehub_shipper.EventStore.get_events_after", return_value=events),
        patch("zerg.services.lifehub_shipper.httpx.AsyncClient", return_value=mock_client),
    ):
        await ship_run_to_lifehub(100, "trace-prod")

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["device_id"] == "zerg-prod"


@pytest.mark.asyncio
async def test_ship_run_skips_duplicate():
    """Same run_id should not be shipped twice."""
    now = datetime(2026, 1, 18, 0, 0, 0, tzinfo=timezone.utc)
    events = [_FakeEvent("supervisor_started", {"task": "hi"}, now)]

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))

    with (
        patch("zerg.services.lifehub_shipper.get_settings", return_value=_settings()),
        patch("zerg.services.lifehub_shipper.db_session", _fake_db_session),
        patch("zerg.services.lifehub_shipper.EventStore.get_events_after", return_value=events),
        patch("zerg.services.lifehub_shipper.httpx.AsyncClient", return_value=mock_client),
    ):
        # First call should succeed
        await ship_run_to_lifehub(200, "trace-dup")
        assert mock_client.post.call_count == 1

        # Second call with same run_id should be skipped
        await ship_run_to_lifehub(200, "trace-dup")
        assert mock_client.post.call_count == 1  # Still 1, not 2


def test_redact_sensitive_fields():
    """Test that sensitive fields are properly redacted."""
    payload = {
        "task": "do something",
        "api_key": "secret-key-123",
        "password": "hunter2",
        "nested": {
            "access_token": "token-abc",
            "safe_field": "visible",
        },
        "list_data": [
            {"secret": "hidden"},
            {"public": "shown"},
        ],
    }

    redacted = _redact_sensitive_fields(payload)

    assert redacted["task"] == "do something"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["nested"]["access_token"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "visible"
    assert redacted["list_data"][0]["secret"] == "[REDACTED]"
    assert redacted["list_data"][1]["public"] == "shown"


def test_redact_handles_non_dict():
    """Redaction should handle non-dict inputs gracefully."""
    assert _redact_sensitive_fields("string") == "string"
    assert _redact_sensitive_fields(123) == 123
    assert _redact_sensitive_fields(None) is None
    assert _redact_sensitive_fields([1, 2, 3]) == [1, 2, 3]
