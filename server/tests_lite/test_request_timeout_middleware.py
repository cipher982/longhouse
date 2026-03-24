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
