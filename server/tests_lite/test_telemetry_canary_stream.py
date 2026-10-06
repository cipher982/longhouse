from __future__ import annotations

import json
import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

pytest_plugins = ("tests_lite.live_catalog_harness",)


from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.routers.telemetry import canary_router
from zerg.routers.telemetry import require_canary_token


def _app(*, authenticated_owner: int | None = 7) -> FastAPI:
    app = FastAPI()
    app.include_router(canary_router)
    app.dependency_overrides[require_single_tenant] = lambda: None
    if authenticated_owner is not None:
        app.dependency_overrides[verify_agents_caller] = lambda: SimpleNamespace(owner_id=authenticated_owner)
    return app


def _authed_app(owner_id: int = 7) -> FastAPI:
    app = _app(authenticated_owner=owner_id)
    app.dependency_overrides[require_canary_token] = lambda: None
    return app


def test_canary_stream_requires_both_authentication_factors(monkeypatch, live_catalog):
    session_id = str(uuid4())
    monkeypatch.setenv("LONGHOUSE_CANARY_TOKEN", "canary-secret")

    with live_catalog.http_client():
        response = TestClient(_app(authenticated_owner=None)).get(
            f"/telemetry/canary-stream?session_id={session_id}",
            headers={"X-Canary-Token": "canary-secret"},
        )

    assert response.status_code == 401


def test_canary_stream_rejects_missing_canary_token(monkeypatch):
    monkeypatch.delenv("LONGHOUSE_CANARY_TOKEN", raising=False)
    response = TestClient(_app()).get(f"/telemetry/canary-stream?session_id={uuid4()}")

    assert response.status_code == 401


def test_canary_stream_denies_cross_owner_session(monkeypatch, live_catalog):
    monkeypatch.setenv("LONGHOUSE_CANARY_TOKEN", "canary-secret")
    owner_id = live_catalog.create_user("canary-owner@longhouse.test")
    foreign_owner_id = live_catalog.create_user("canary-foreign@longhouse.test")
    owner_token = live_catalog.create_device_token(owner_id=owner_id, device_id="cube-canary")
    foreign_token = live_catalog.create_device_token(owner_id=foreign_owner_id, device_id="foreign-canary")
    session_id = uuid4()
    envelope = live_catalog.envelope_body(
        session_id=session_id,
        device_id="cube-canary",
        provider="canary",
        project="canary",
        texts=("canary bootstrap",),
        now=datetime.now(timezone.utc),
    )
    with live_catalog.http_client() as client:
        created = client.post(
            "/agents/storage/v2/envelopes",
            json=envelope,
            headers={"X-Agents-Token": owner_token, "X-Longhouse-Storage-Lane": "live"},
        )
        assert created.status_code == 200, created.text
        response = client.get(
            f"/telemetry/canary-stream?session_id={session_id}",
            headers={"X-Agents-Token": foreign_token, "X-Canary-Token": "canary-secret"},
        )
    assert response.status_code == 404


def test_canary_stream_denies_noncanary_provider(monkeypatch):
    session_id = uuid4()
    monkeypatch.setattr(
        "zerg.services.live_catalog_timeline.read_live_catalog_session",
        lambda *_args, **_kwargs: (SimpleNamespace(provider="claude"), "claude-session", "12"),
    )

    response = TestClient(_authed_app()).get(f"/telemetry/canary-stream?session_id={session_id}")

    assert response.status_code == 404


def test_canary_stream_correlates_producer_seq_and_never_leaks_workspace_content(monkeypatch):
    session_id = uuid4()
    emitted_at_ms = 1_800_000_000_000
    monkeypatch.setattr(
        "zerg.services.live_catalog_timeline.read_live_catalog_session",
        lambda *_args, **_kwargs: (SimpleNamespace(provider="canary"), "canary-session", "12"),
    )

    async def workspace_stream(_request, **_kwargs):
        yield {"event": "connected", "data": json.dumps({"session_id": str(session_id), "server_now_ms": 100})}
        yield {
            "event": "workspace_changed",
            "id": "12",
            "data": json.dumps({"session_id": str(session_id), "pubsub_seq": 12, "server_now_ms": 101}),
        }
        yield {
            "event": "workspace_changed",
            "id": "13",
            "data": json.dumps(
                {
                    "session_id": str(session_id),
                    "change_kind": "runtime",
                    "latest_event_id": 99,
                    "transcript_preview": {"text": "private transcript"},
                    "tool_name": "private_tool",
                    "canary_seq": 24,
                    "canary_emitted_at_ms": emitted_at_ms,
                    "server_fanout_at_ms": emitted_at_ms + 50,
                    "server_now_ms": emitted_at_ms + 55,
                    "pubsub_seq": 13,
                }
            ),
        }

    monkeypatch.setattr("zerg.routers.timeline._live_catalog_workspace_stream", workspace_stream)

    response = TestClient(_authed_app()).get(f"/telemetry/canary-stream?session_id={session_id}")

    assert response.status_code == 200
    assert "private transcript" not in response.text
    assert "transcript_preview" not in response.text
    assert "tool_name" not in response.text
    frames = response.text.replace("\r\n", "\n").split("\n\n")
    observed = next(frame for frame in frames if "event: canary_observation" in frame)
    data = json.loads(next(line[6:] for line in observed.splitlines() if line.startswith("data: ")))
    assert data == {
        "canary_seq": 24,
        "canary_emitted_at_ms": emitted_at_ms,
        "server_fanout_at_ms": emitted_at_ms + 50,
        "server_now_ms": emitted_at_ms + 55,
        "pubsub_seq": 13,
    }
    assert "latest_event_id" not in data
    assert "id: 13" in observed


@pytest.mark.parametrize("field", ["server_fanout_at_ms", "server_now_ms"])
def test_canary_stream_rejects_non_positive_server_timing(monkeypatch, field):
    session_id = uuid4()
    monkeypatch.setattr(
        "zerg.services.live_catalog_timeline.read_live_catalog_session",
        lambda *_args, **_kwargs: (SimpleNamespace(provider="canary"), "canary-session", "12"),
    )
    marker = {
        "canary_seq": 24,
        "canary_emitted_at_ms": 1_800_000_000_000,
        "server_fanout_at_ms": 1_800_000_000_050,
        "server_now_ms": 1_800_000_000_055,
        "pubsub_seq": 13,
    }
    marker[field] = 0

    async def workspace_stream(_request, **_kwargs):
        yield {"event": "workspace_changed", "data": json.dumps(marker)}

    monkeypatch.setattr("zerg.routers.timeline._live_catalog_workspace_stream", workspace_stream)
    response = TestClient(_authed_app()).get(f"/telemetry/canary-stream?session_id={session_id}")
    assert "event: error" in response.text
    assert "event: canary_observation" not in response.text
