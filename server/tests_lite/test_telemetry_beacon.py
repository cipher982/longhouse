"""Telemetry client-render beacon smoke tests."""

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.dependencies.auth import require_admin
from zerg.routers import telemetry as telemetry_mod
from zerg.routers.telemetry import admin_router
from zerg.routers.telemetry import beacon_router


def _client() -> TestClient:
    telemetry_mod._samples.clear()
    telemetry_mod._buckets.clear()
    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    app.include_router(beacon_router)
    app.include_router(admin_router)
    return TestClient(app)


def _beacon(**kwargs):
    base = {
        "event_id": "evt-1",
        "session_id": "sess-1",
        "surface": "web",
        "managed": True,
        "emitted_at_ms": int(time.time() * 1000) - 150,
        "rendered_at_ms": int(time.time() * 1000),
        "clock_skew_ms": 0,
    }
    base.update(kwargs)
    return base


def test_beacon_accepts_single_and_batch():
    c = _client()

    resp = c.post("/telemetry/client-render", json=_beacon())
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1

    resp = c.post("/telemetry/client-render", json=[_beacon(event_id="a"), _beacon(event_id="b", surface="ios")])
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2


def test_beacon_drops_implausible_clock_skew():
    c = _client()
    resp = c.post("/telemetry/client-render", json=_beacon(clock_skew_ms=120_000))
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 0
    assert resp.json()["dropped_skew"] == 1


def test_beacon_drops_stale_and_negative():
    c = _client()
    now = int(time.time() * 1000)
    # 2 minutes stale
    resp = c.post(
        "/telemetry/client-render",
        json=_beacon(emitted_at_ms=now - 120_000, rendered_at_ms=now),
    )
    assert resp.json()["dropped_range"] == 1

    # Strongly negative (emitted in the future beyond clamp)
    resp = c.post(
        "/telemetry/client-render",
        json=_beacon(emitted_at_ms=now + 10_000, rendered_at_ms=now),
    )
    assert resp.json()["dropped_range"] == 1


def test_beacon_rate_limited_per_ip(monkeypatch):
    monkeypatch.setattr(telemetry_mod.time, "monotonic", lambda: 1000.0)
    c = _client()
    # Burst capacity is 60; request the 61st should 429.
    for _ in range(60):
        r = c.post("/telemetry/client-render", json=_beacon())
        assert r.status_code == 200
    r = c.post("/telemetry/client-render", json=_beacon())
    assert r.status_code == 429


def test_beacon_rate_limit_refills_over_time(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(telemetry_mod.time, "monotonic", lambda: now)
    c = _client()

    for _ in range(60):
        assert c.post("/telemetry/client-render", json=_beacon()).status_code == 200
    assert c.post("/telemetry/client-render", json=_beacon()).status_code == 429

    now += 1.0
    for _ in range(20):
        assert c.post("/telemetry/client-render", json=_beacon()).status_code == 200
    assert c.post("/telemetry/client-render", json=_beacon()).status_code == 429


def test_latency_summary_groups_by_surface_and_managed():
    c = _client()
    now = int(time.time() * 1000)
    c.post(
        "/telemetry/client-render",
        json=[
            _beacon(emitted_at_ms=now - 100, rendered_at_ms=now, surface="web", managed=True),
            _beacon(emitted_at_ms=now - 200, rendered_at_ms=now, surface="web", managed=True),
            _beacon(emitted_at_ms=now - 400, rendered_at_ms=now, surface="ios", managed=False),
        ],
    )
    summary = c.get("/telemetry/latency-summary").json()
    groups = {(g["surface"], g["managed"]): g for g in summary["groups"]}
    assert ("web", True) in groups
    assert ("ios", False) in groups
    assert groups[("web", True)]["count"] == 2
    assert groups[("ios", False)]["p50_ms"] >= 300
