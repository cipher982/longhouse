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
    telemetry_mod._canary_last_obs_monotonic.clear()
    # Also reset the canary seq gauge — it's process-global in prometheus_client.
    try:
        from zerg.metrics import canary_seq_last_seen as _gauge

        for hop in ("ingest", "sse", "render"):
            _gauge.labels(hop=hop).set(0)
    except Exception:
        pass
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


def test_canary_observation_endpoint_records():
    c = _client()
    resp = c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 42, "hop": "sse", "surface": "observer", "latency_ms": 125},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["hop"] == "sse"
    assert body["seq"] == 42
    # canary_last_obs_age_s helper should report a very fresh age
    from zerg.routers.telemetry import canary_last_obs_age_s

    age = canary_last_obs_age_s("sse")
    assert age is not None and age < 5.0


def test_canary_observation_rejects_invalid_hop():
    c = _client()
    resp = c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 1, "hop": "bogus", "latency_ms": 10},
    )
    assert resp.status_code == 422


def test_canary_observation_rejects_negative_latency():
    c = _client()
    resp = c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 1, "hop": "sse", "latency_ms": -5},
    )
    assert resp.status_code == 422


def test_selfcheck_reports_all_hops_dead_when_no_observations():
    c = _client()
    resp = c.get("/telemetry/selfcheck")
    assert resp.status_code == 200
    body = resp.json()
    for hop in ("ingest", "sse", "render"):
        assert hop in body["hops"]
        assert body["hops"][hop]["last_obs_age_s"] is None
        assert body["hops"][hop]["alive"] is False
    # Required hops (ingest, sse) must be alive. None = not alive -> ok false.
    assert body["ok"] is False


def test_selfcheck_ok_when_required_alive_even_if_render_absent():
    c = _client()
    c.post("/telemetry/canary-observation", json={"canary_seq": 1, "hop": "ingest", "latency_ms": 25})
    c.post("/telemetry/canary-observation", json={"canary_seq": 1, "hop": "sse", "latency_ms": 150})
    body = c.get("/telemetry/selfcheck").json()
    # render never observed but it's optional
    assert body["hops"]["render"]["alive"] is False
    assert body["hops"]["render"]["required"] is False
    assert body["ok"] is True


def test_selfcheck_reports_recent_hops_alive():
    c = _client()
    c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 1, "hop": "ingest", "latency_ms": 25},
    )
    c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 1, "hop": "sse", "latency_ms": 150},
    )
    body = c.get("/telemetry/selfcheck").json()
    assert body["hops"]["ingest"]["alive"] is True
    assert body["hops"]["sse"]["alive"] is True
    assert body["hops"]["render"]["alive"] is False
    assert body["seq"]["ingest"] == 1
    assert body["seq"]["sse"] == 1
    assert body["seq"]["gap"] == 0


def test_selfcheck_flags_seq_gap():
    c = _client()
    # Producer way ahead of observer
    c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 100, "hop": "ingest", "latency_ms": 25},
    )
    c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 10, "hop": "sse", "latency_ms": 150},
    )
    body = c.get("/telemetry/selfcheck").json()
    assert body["seq"]["gap"] == 90
    # gap >= 10 trips overall ok=False
    assert body["ok"] is False


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
