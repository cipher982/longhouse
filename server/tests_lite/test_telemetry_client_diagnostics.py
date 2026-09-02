"""Client diagnostics beacon: the phone's own lifecycle marks land server-side.

The app already logs stream connects, stalls, polls and reconciles to OSLog,
where nobody can read them without a cable and root. This route makes those
marks readable next to the server's log for the same session.
"""

import os
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.dependencies.auth import require_admin
from zerg.routers import telemetry as telemetry_mod
from zerg.routers.telemetry import admin_router
from zerg.routers.telemetry import beacon_router


def _client() -> TestClient:
    telemetry_mod._diagnostics.clear()
    telemetry_mod._buckets.clear()
    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(beacon_router)
    app.include_router(admin_router)
    return TestClient(app)


def _batch(**overrides):
    now = int(time.time() * 1000)
    base = {
        "surface": "ios",
        "device_label": "olive",
        "app_build": "0.1.46-dev+abc1234",
        "entries": [
            {"at_ms": now - 500, "stage": "stream_connected", "session_id": "sess-1"},
            {"at_ms": now, "stage": "stream_stale", "detail": "stale_after_s=45", "session_id": "sess-1"},
            {"at_ms": now, "stage": "poll_tail", "detail": "connected=false", "session_id": "sess-2"},
        ],
    }
    base.update(overrides)
    return base


def test_batch_is_accepted_and_logged(caplog):
    c = _client()
    with caplog.at_level("INFO", logger="longhouse.client_diag"):
        resp = c.post("/telemetry/client-diagnostics", json=_batch())
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 3}
    lines = [record.getMessage() for record in caplog.records if record.name == "longhouse.client_diag"]
    assert len(lines) == 3
    assert "device=olive" in lines[1]
    assert "stage=stream_stale stale_after_s=45" in lines[1]
    assert "session=sess-1" in lines[1]


def test_recent_filters_by_session_and_keeps_order():
    c = _client()
    assert c.post("/telemetry/client-diagnostics", json=_batch()).status_code == 200
    rows = c.get("/telemetry/client-diagnostics/recent", params={"session_id": "sess-1"}).json()
    assert [row["stage"] for row in rows] == ["stream_connected", "stream_stale"]
    assert rows[0]["device_label"] == "olive"
    assert rows[0]["app_build"] == "0.1.46-dev+abc1234"
    assert c.get("/telemetry/client-diagnostics/recent", params={"limit": 1}).json()[0]["stage"] == "poll_tail"


def test_rejects_oversized_batch():
    c = _client()
    now = int(time.time() * 1000)
    entries = [{"at_ms": now, "stage": "poll_tail"} for _ in range(201)]
    resp = c.post("/telemetry/client-diagnostics", json=_batch(entries=entries))
    assert resp.status_code == 422
