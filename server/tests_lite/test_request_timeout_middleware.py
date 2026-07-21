from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.metrics import product_read_requests_total
from zerg.middleware.request_timeout import RequestTimeoutMiddleware
from zerg.middleware.request_timeout import _product_read_route_class
from zerg.middleware import request_timeout as request_timeout_module


def _counter_value(route_class: str, status_family: str, outcome: str) -> float:
    return product_read_requests_total.labels(route_class, status_family, outcome)._value.get()


def test_product_read_route_classes_are_bounded_and_ignore_identifiers():
    session_id = "7eb5789a-b99c-4b51-859b-6df10dbc67f0"
    assert _product_read_route_class("/timeline/sessions", "GET") == "timeline"
    assert _product_read_route_class(f"/timeline/sessions/{session_id}", "GET") == "session_detail"
    assert _product_read_route_class(f"/timeline/sessions/{session_id}/workspace", "GET") == "session_detail"
    assert _product_read_route_class(f"/agents/storage/v2/sessions/{session_id}/raw", "GET") == "raw_export"
    assert _product_read_route_class("/agents/recall", "GET") == "recall"
    assert _product_read_route_class("/timeline/sessions/semantic", "GET") == "search"
    assert _product_read_route_class(f"/agents/sessions/{session_id}/action", "POST") is None
    assert _product_read_route_class("/unrelated/private-session-id", "GET") is None


@pytest.mark.parametrize(
    "path",
    [
        "/agents/sessions/summary",
        "/agents/sessions/active",
        "/agents/sessions/wall",
        "/timeline/sessions/summary",
        "/timeline/sessions/not-a-uuid/workspace",
        "/timeline/sessions/7eb5789a-b99c-4b51-859b-6df10dbc67f0/preview",
        "/agents/sessions/7eb5789a-b99c-4b51-859b-6df10dbc67f0/tail",
    ],
)
def test_product_read_route_classes_exclude_auxiliary_and_collection_routes(path):
    assert _product_read_route_class(path, "GET") is None


def test_product_read_success_is_retained_by_route_class():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=1)

    @app.get("/api/timeline/sessions")
    async def timeline_sessions():
        return {"sessions": []}

    before = _counter_value("timeline", "2xx", "ok")
    with TestClient(app) as client:
        response = client.get("/api/timeline/sessions")

    assert response.status_code == 200
    assert _counter_value("timeline", "2xx", "ok") == before + 1


def test_product_read_timeout_is_retained(monkeypatch):
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=1)
    monkeypatch.setitem(request_timeout_module._TIMEOUT_OVERRIDES, "/agents/recall", 0.01)

    @app.get("/api/agents/recall")
    async def recall():
        await asyncio.sleep(0.05)
        return {"matches": []}

    before = _counter_value("recall", "5xx", "timeout")
    with TestClient(app) as client:
        response = client.get("/api/agents/recall")

    assert response.status_code == 503
    assert _counter_value("recall", "5xx", "timeout") == before + 1


def test_request_timeout_returns_503_for_normal_api_route():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.get("/api/slow")
    async def slow_route():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/api/slow")

    assert response.status_code == 503
    assert response.json() == {"detail": "Request timed out"}


def test_managed_local_launch_route_uses_longer_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.post("/api/sessions/managed-local/this-device")
    async def managed_local_launch():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/api/sessions/managed-local/this-device")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_removed_remote_launch_route_has_no_special_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.post("/api/sessions/launch")
    async def remote_session_launch():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/api/sessions/launch")

    assert response.status_code == 503


def test_agents_ingest_route_uses_longer_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.post("/api/agents/ingest")
    async def ingest():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/api/agents/ingest")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_agents_archive_bundle_route_uses_longer_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.get("/api/agents/sessions/test-session/archive-bundle")
    async def archive_bundle():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/api/agents/sessions/test-session/archive-bundle")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_archive_backed_user_read_uses_longer_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.get("/api/agents/worklog/day")
    async def worklog():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/api/agents/worklog/day")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_session_control_write_keeps_default_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.post("/api/agents/sessions/test-session/action")
    async def session_action():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/api/agents/sessions/test-session/action")

    assert response.status_code == 503


def test_provider_live_proof_route_uses_default_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.post("/api/agents/machines/cinder/provider-live-proof")
    async def provider_live_proof():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/api/agents/machines/cinder/provider-live-proof")

    assert response.status_code == 503
    assert response.json() == {"detail": "Request timed out"}
