"""Telemetry client-render beacon smoke tests."""

import time
from datetime import datetime
from datetime import timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import require_admin
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.routers import telemetry as telemetry_mod
from zerg.routers.telemetry import admin_router
from zerg.routers.telemetry import beacon_router
from zerg.routers.telemetry import canary_router
from zerg.routers.telemetry import require_canary_token
from zerg.services.session_observations import OBS_KIND_CLIENT_RENDER


def _client() -> tuple[TestClient, sessionmaker]:
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
    engine = make_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[require_canary_token] = lambda: None
    app.dependency_overrides[get_db] = override_db
    app.include_router(beacon_router)
    app.include_router(admin_router)
    app.include_router(canary_router)
    return TestClient(app), factory


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
    c, _factory = _client()

    resp = c.post("/telemetry/client-render", json=_beacon())
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1

    resp = c.post("/telemetry/client-render", json=[_beacon(event_id="a"), _beacon(event_id="b", surface="ios")])
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2


def test_beacon_drops_implausible_clock_skew():
    c, _factory = _client()
    resp = c.post("/telemetry/client-render", json=_beacon(clock_skew_ms=120_000))
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 0
    assert resp.json()["dropped_skew"] == 1


def test_beacon_drops_stale_and_negative():
    c, _factory = _client()
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
    c, _factory = _client()
    # Burst capacity is 60; request the 61st should 429.
    for _ in range(60):
        r = c.post("/telemetry/client-render", json=_beacon())
        assert r.status_code == 200
    r = c.post("/telemetry/client-render", json=_beacon())
    assert r.status_code == 429


def test_beacon_rate_limit_refills_over_time(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(telemetry_mod.time, "monotonic", lambda: now)
    c, _factory = _client()

    for _ in range(60):
        assert c.post("/telemetry/client-render", json=_beacon()).status_code == 200
    assert c.post("/telemetry/client-render", json=_beacon()).status_code == 429

    now += 1.0
    for _ in range(20):
        assert c.post("/telemetry/client-render", json=_beacon()).status_code == 200
    assert c.post("/telemetry/client-render", json=_beacon()).status_code == 429


def test_canary_token_auth_required_when_no_override(monkeypatch):
    """Without the dependency override, real token auth kicks in."""
    app = FastAPI()
    app.include_router(canary_router)
    client = TestClient(app)

    # No env -> no token set -> anything rejected
    monkeypatch.delenv("LONGHOUSE_CANARY_TOKEN", raising=False)
    r = client.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 1, "hop": "sse", "latency_ms": 50},
    )
    assert r.status_code == 401

    # Set env, retry with matching header -> 200
    monkeypatch.setenv("LONGHOUSE_CANARY_TOKEN", "test-secret-123")
    r = client.post(
        "/telemetry/canary-observation",
        headers={"X-Canary-Token": "test-secret-123"},
        json={"canary_seq": 1, "hop": "sse", "latency_ms": 50},
    )
    assert r.status_code == 200
    # Wrong token -> 401
    r = client.post(
        "/telemetry/canary-observation",
        headers={"X-Canary-Token": "wrong"},
        json={"canary_seq": 1, "hop": "sse", "latency_ms": 50},
    )
    assert r.status_code == 401


def test_canary_observation_endpoint_records():
    c, _factory = _client()
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
    c, _factory = _client()
    resp = c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 1, "hop": "bogus", "latency_ms": 10},
    )
    assert resp.status_code == 422


def test_canary_observation_rejects_negative_latency():
    c, _factory = _client()
    resp = c.post(
        "/telemetry/canary-observation",
        json={"canary_seq": 1, "hop": "sse", "latency_ms": -5},
    )
    assert resp.status_code == 422


def test_selfcheck_reports_all_hops_dead_when_no_observations():
    c, _factory = _client()
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
    c, _factory = _client()
    c.post("/telemetry/canary-observation", json={"canary_seq": 1, "hop": "ingest", "latency_ms": 25})
    c.post("/telemetry/canary-observation", json={"canary_seq": 1, "hop": "sse", "latency_ms": 150})
    body = c.get("/telemetry/selfcheck").json()
    # render never observed but it's optional
    assert body["hops"]["render"]["alive"] is False
    assert body["hops"]["render"]["required"] is False
    assert body["ok"] is True


def test_selfcheck_reports_recent_hops_alive():
    c, _factory = _client()
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
    c, _factory = _client()
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
    c, _factory = _client()
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


def test_beacon_persists_queryable_render_observation():
    c, factory = _client()
    session_id = UUID("aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb")
    now = int(time.time() * 1000)
    with factory() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                device_id="cinder",
                environment="test",
                project="telemetry",
                started_at=datetime.now(timezone.utc),
                user_messages=1,
                assistant_messages=1,
            )
        )
        db.commit()

    resp = c.post(
        "/telemetry/client-render",
        json=_beacon(
            session_id=str(session_id),
            event_id="123",
            emitted_at_ms=now - 250,
            rendered_at_ms=now,
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1

    with factory() as db:
        observation = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session_id)
            .filter(SessionObservation.kind == OBS_KIND_CLIENT_RENDER)
            .one()
        )
        assert observation.provider == "claude"
        assert observation.device_id == "cinder"
        assert observation.source == "client_render_beacon"
        assert observation.source_cursor == "event:123"

    recent = c.get(f"/telemetry/client-render/recent?session_id={session_id}&event_id=123").json()
    assert recent["items"] == [
        {
            "session_id": str(session_id),
            "event_id": "123",
            "surface": "web",
            "managed": True,
            "latency_ms": 250,
            "emitted_at_ms": now - 250,
            "rendered_at_ms": now,
            "clock_skew_ms": 0,
            "webkit": None,
            "observed_at": recent["items"][0]["observed_at"],
            "received_at": recent["items"][0]["received_at"],
        }
    ]


def test_beacon_persists_ios_webkit_diagnostics():
    c, factory = _client()
    session_id = UUID("aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb")
    now = int(time.time() * 1000)
    with factory() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                device_id="cinder",
                environment="test",
                project="telemetry",
                started_at=datetime.now(timezone.utc),
                user_messages=1,
                assistant_messages=1,
            )
        )
        db.commit()

    webkit = {
        "stage": "rendered",
        "payload_byte_size": 4096,
        "row_count": 18,
        "latest_item_id": "assistant:123",
        "render_sequence": 7,
        "js_failure_count": 0,
        "should_stick_to_bottom": True,
        "web_view_loaded": True,
    }
    resp = c.post(
        "/telemetry/client-render",
        json=_beacon(
            session_id=str(session_id),
            event_id="123",
            surface="ios",
            emitted_at_ms=now - 250,
            rendered_at_ms=now,
            webkit=webkit,
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1

    recent = c.get(f"/telemetry/client-render/recent?session_id={session_id}&event_id=123").json()
    assert recent["items"][0]["surface"] == "ios"
    assert recent["items"][0]["webkit"] == webkit
