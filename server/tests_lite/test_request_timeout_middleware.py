from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.middleware.request_timeout import RequestTimeoutMiddleware


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


def test_remote_session_launch_route_uses_longer_timeout_budget():
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout=0.01)

    @app.post("/api/sessions/launch")
    async def remote_session_launch():
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/api/sessions/launch")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


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
