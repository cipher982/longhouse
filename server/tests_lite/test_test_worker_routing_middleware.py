from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.database import get_test_worker_id
from zerg.middleware.test_worker_routing import E2EWorkerRoutingMiddleware


async def _read_worker_id() -> str | None:
    await asyncio.sleep(0)
    return get_test_worker_id()


def _make_app(*, enabled: bool = True) -> FastAPI:
    app = FastAPI()
    app.add_middleware(E2EWorkerRoutingMiddleware, enabled=enabled)

    @app.get("/worker")
    async def worker() -> dict[str, str | None]:
        task_worker_id = await asyncio.create_task(_read_worker_id())
        return {
            "worker_id": get_test_worker_id(),
            "task_worker_id": task_worker_id,
        }

    return app


def test_test_worker_header_sets_request_context_and_resets_afterward():
    app = _make_app()

    with TestClient(app) as client:
        response = client.get("/worker", headers={"X-Test-Worker": "worker-7"})
        assert response.status_code == 200
        assert response.json() == {
            "worker_id": "worker-7",
            "task_worker_id": "worker-7",
        }

        follow_up = client.get("/worker")
        assert follow_up.status_code == 200
        assert follow_up.json() == {
            "worker_id": None,
            "task_worker_id": None,
        }

    assert get_test_worker_id() is None


def test_disabled_test_worker_middleware_ignores_header():
    app = _make_app(enabled=False)

    with TestClient(app) as client:
        response = client.get("/worker", headers={"X-Test-Worker": "worker-7"})

    assert response.status_code == 200
    assert response.json() == {
        "worker_id": None,
        "task_worker_id": None,
    }
