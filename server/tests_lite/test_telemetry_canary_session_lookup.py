"""Telemetry canary-session lookup routing under live catalog vs legacy."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.catalogd.client import CatalogUnavailable
from zerg.routers.telemetry import canary_router
from zerg.routers.telemetry import require_canary_token


class _FakeCatalog:
    def __init__(self, *, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.result


def test_canary_session_lookup_uses_catalog_when_live_enabled(monkeypatch):
    session_id = str(uuid4())
    catalog = _FakeCatalog(
        result={
            "session_id": session_id,
            "provider": "canary",
            "commit_seq": "12",
            "max_age_seconds": 300,
        }
    )
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.get_catalogd_client",
        lambda: catalog,
    )
    app = FastAPI()
    app.include_router(canary_router)
    app.dependency_overrides[require_canary_token] = lambda: None
    client = TestClient(app)

    response = client.get("/telemetry/canary-session")
    assert response.status_code == 200
    assert response.json() == {"session_id": session_id}
    assert catalog.calls == [
        (
            "storage.session.canary.lookup.v2",
            {
                "observed_at": catalog.calls[0][1]["observed_at"],
                "max_age_seconds": 300,
            },
        )
    ]
    datetime.fromisoformat(catalog.calls[0][1]["observed_at"])


def test_canary_session_lookup_returns_503_when_catalog_unavailable(monkeypatch):
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.get_catalogd_client",
        lambda: None,
    )
    app = FastAPI()
    app.include_router(canary_router)
    app.dependency_overrides[require_canary_token] = lambda: None
    client = TestClient(app)

    response = client.get("/telemetry/canary-session")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_unavailable"


def test_canary_session_lookup_returns_503_on_catalog_error(monkeypatch):
    catalog = _FakeCatalog(error=CatalogUnavailable("down"))
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.get_catalogd_client",
        lambda: catalog,
    )
    app = FastAPI()
    app.include_router(canary_router)
    app.dependency_overrides[require_canary_token] = lambda: None
    client = TestClient(app)

    response = client.get("/telemetry/canary-session")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_unavailable"
    assert catalog.calls


def test_canary_session_lookup_returns_503_on_invalid_catalog_session_id(monkeypatch):
    catalog = _FakeCatalog(result={"session_id": "not-a-uuid"})
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.get_catalogd_client",
        lambda: catalog,
    )
    app = FastAPI()
    app.include_router(canary_router)
    app.dependency_overrides[require_canary_token] = lambda: None
    client = TestClient(app)

    response = client.get("/telemetry/canary-session")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_unavailable"
