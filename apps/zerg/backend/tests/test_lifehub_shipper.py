import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from zerg.services.lifehub_shipper import ship_run_to_lifehub


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
    }
    data.update(overrides)
    return SimpleNamespace(**data)


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
async def test_ship_run_skips_without_api_key():
    with (
        patch("zerg.services.lifehub_shipper.get_settings", return_value=_settings(lifehub_api_key=None)),
        patch("zerg.services.lifehub_shipper.EventStore.get_events_after") as mock_get,
        patch("zerg.services.lifehub_shipper.httpx.AsyncClient") as mock_client,
    ):
        await ship_run_to_lifehub(123, "trace-xyz")

        mock_get.assert_not_called()
        mock_client.assert_not_called()


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

    shipped_events = kwargs["json"]["events"]
    # supervisor_token + run_updated should be filtered out
    assert len(shipped_events) == 3

    first = json.loads(shipped_events[0]["raw_text"])
    assert first["event_type"] == "supervisor_started"
    assert first["payload"] == {"task": "hi"}
    assert shipped_events[0]["timestamp"] == now.isoformat()
