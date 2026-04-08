from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.database import get_test_commis_id
from zerg.middleware.test_commis_routing import E2ECommisRoutingMiddleware


async def _read_commis_id() -> str | None:
    await asyncio.sleep(0)
    return get_test_commis_id()


def _make_app(*, enabled: bool = True) -> FastAPI:
    app = FastAPI()
    app.add_middleware(E2ECommisRoutingMiddleware, enabled=enabled)

    @app.get("/commis")
    async def commis() -> dict[str, str | None]:
        task_commis_id = await asyncio.create_task(_read_commis_id())
        return {
            "commis_id": get_test_commis_id(),
            "task_commis_id": task_commis_id,
        }

    return app


def test_test_commis_header_sets_request_context_and_resets_afterward():
    app = _make_app()

    with TestClient(app) as client:
        response = client.get("/commis", headers={"X-Test-Commis": "worker-7"})
        assert response.status_code == 200
        assert response.json() == {
            "commis_id": "worker-7",
            "task_commis_id": "worker-7",
        }

        follow_up = client.get("/commis")
        assert follow_up.status_code == 200
        assert follow_up.json() == {
            "commis_id": None,
            "task_commis_id": None,
        }

    assert get_test_commis_id() is None


def test_disabled_test_commis_middleware_ignores_header():
    app = _make_app(enabled=False)

    with TestClient(app) as client:
        response = client.get("/commis", headers={"X-Test-Commis": "worker-7"})

    assert response.status_code == 200
    assert response.json() == {
        "commis_id": None,
        "task_commis_id": None,
    }
