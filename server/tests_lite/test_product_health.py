"""Product health checks over persisted session observations."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

import zerg.services.agent_heartbeat_health as heartbeat_health
import zerg.services.product_health as product_health
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.services.product_health import build_live_preview_check
from zerg.services.product_health import build_product_health_checks
from zerg.services.session_observations import OBS_KIND_CLIENT_RENDER
from zerg.services.session_observations import SOURCE_DOMAIN_CLIENT
from zerg.services.session_observations import record_session_observation

PINNED_NOW = datetime(2026, 5, 22, 18, 0, 0, tzinfo=timezone.utc)


def _make_db(tmp_path):
    db_path = tmp_path / "test_product_health.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_client(SessionLocal):
    def override_get_db():
        with SessionLocal() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1,
        email="owner@example.com",
        role="USER",
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def _seed_session(db, *, provider: str = "codex", device_id: str = "cinder") -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="test",
        project="zerg",
        device_id=device_id,
        provider_session_id=str(uuid4()),
        managed_transport="codex_app_server",
        started_at=PINNED_NOW - timedelta(minutes=30),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_heartbeat(
    db,
    *,
    device_id: str = "cinder",
    received_delta_seconds: int = 60,
    **kwargs,
) -> None:
    values = {
        "device_id": device_id,
        "received_at": PINNED_NOW - timedelta(seconds=received_delta_seconds),
        "version": "0.1.16-test",
        "last_ship_result": "ok",
        "ship_attempts_1h": 1,
        "ship_successes_1h": 1,
        "raw_json": '{"ship_attempts_10m":1,"ship_successes_10m":1}',
    }
    values.update(kwargs)
    db.add(AgentHeartbeat(**values))
    db.commit()


def _record_render(
    db,
    *,
    session_id: UUID,
    provider: str = "codex",
    surface: str = "web",
    managed: bool = True,
    latency_ms: int = 100,
    observed_delta_seconds: int = 60,
    webkit: dict | None = None,
    event_id: str | None = None,
) -> None:
    event_id = event_id or str(uuid4())
    payload = {
        "event_id": event_id,
        "surface": surface,
        "managed": managed,
        "emitted_at_ms": 1_779_000_000_000,
        "rendered_at_ms": 1_779_000_000_000 + latency_ms,
        "clock_skew_ms": 0,
        "server_fanout_at_ms": 1_779_000_000_000,
        "client_received_at_ms": 1_779_000_000_020,
        "pubsub_seq": 1,
        "latency_ms": latency_ms,
    }
    if webkit is not None:
        payload["webkit"] = webkit
    record_session_observation(
        db,
        observation_id=f"client_render:{surface}:{session_id}:{event_id}",
        session_id=session_id,
        runtime_key=None,
        provider=provider,
        device_id="cinder",
        source_domain=SOURCE_DOMAIN_CLIENT,
        source="client_render_beacon",
        kind=OBS_KIND_CLIENT_RENDER,
        observed_at=PINNED_NOW - timedelta(seconds=observed_delta_seconds),
        payload=payload,
    )
    db.commit()


def _check(payload, check_id: str):
    for check in payload.checks:
        if check.check == check_id:
            return check
    raise AssertionError(f"missing check {check_id}")


def test_live_preview_no_observations_returns_unknown_with_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        report = build_live_preview_check(db, window="15m")

    assert report.window == "15m"
    assert len(report.cells) == 1
    cell = report.cells[0]
    assert cell.verdict == "unknown"
    assert cell.coverage == "none"
    assert cell.missing == ["client_render_observations"]
    assert cell.thresholds.render_p95_ms_ok == 500


def test_product_health_summary_orders_launch_loop_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(heartbeat_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex")
        _seed_heartbeat(db)
        _record_render(db, session_id=session.id, latency_ms=100, event_id="a")
        payload = build_product_health_checks(db, window="15m")

    assert [check.check for check in payload.checks] == [
        "machine_connected",
        "render_freshness",
        "live_preview",
    ]
    assert _check(payload, "machine_connected").verdict == "ok"
    assert _check(payload, "render_freshness").verdict == "ok"
    assert _check(payload, "live_preview").verdict == "ok"


def test_machine_connected_unknown_without_recent_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(heartbeat_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _seed_heartbeat(db, received_delta_seconds=60 * 60)
        payload = build_product_health_checks(db, window="15m")

    check = _check(payload, "machine_connected")
    assert check.verdict == "unknown"
    assert check.coverage == "none"
    assert check.headline == "No machine heartbeats in the last 15m."


def test_machine_connected_degraded_when_recent_machine_needs_attention(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(heartbeat_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _seed_heartbeat(db, device_id="healthy", received_delta_seconds=30)
        _seed_heartbeat(db, device_id="blocked", received_delta_seconds=30, spool_dead=1)
        payload = build_product_health_checks(db, window="15m")

    check = _check(payload, "machine_connected")
    assert check.verdict == "degraded"
    assert check.coverage == "full"
    assert check.headline == "1 of 2 recent machines healthy; 1 needs attention."


def test_machine_connected_fails_when_all_recent_machines_are_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(heartbeat_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _seed_heartbeat(db, device_id="blocked-a", received_delta_seconds=30, spool_dead=1)
        _seed_heartbeat(db, device_id="blocked-b", received_delta_seconds=30, spool_dead=1)
        payload = build_product_health_checks(db, window="15m")

    check = _check(payload, "machine_connected")
    assert check.verdict == "failing"
    assert check.coverage == "full"
    assert check.headline == "All 2 recent machine connections are broken."


def test_render_freshness_degrades_when_latest_beacon_is_old(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(heartbeat_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex")
        _record_render(db, session_id=session.id, latency_ms=100, observed_delta_seconds=10 * 60)
        payload = build_product_health_checks(db, window="15m")

    check = _check(payload, "render_freshness")
    assert check.verdict == "degraded"
    assert check.coverage == "full"
    assert check.headline == "Render beacons are stale; latest arrived 10m ago."


def test_live_preview_healthy_web_stream_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex")
        _record_render(db, session_id=session.id, latency_ms=100, event_id="a")
        _record_render(db, session_id=session.id, latency_ms=200, event_id="b")
        _record_render(db, session_id=session.id, latency_ms=300, event_id="c")
        report = build_live_preview_check(db, window="15m", provider="codex")

    cell = report.cells[0]
    assert cell.dimension.provider == "codex"
    assert cell.dimension.surface == "web"
    assert cell.dimension.managed is True
    assert cell.verdict == "ok"
    assert cell.coverage == "full"
    assert cell.signals.events == 3
    assert cell.signals.render_p95_ms == 290
    assert cell.evidence_refs[0].kind == "session"
    assert cell.evidence_refs[0].reason == "highest_latency"


def test_live_preview_ios_without_render_duration_is_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex")
        _record_render(
            db,
            session_id=session.id,
            surface="ios",
            latency_ms=240,
            webkit={
                "stage": "rendered",
                "payload_byte_size": 4096,
                "row_count": 18,
                "render_sequence": 7,
                "js_failure_count": 0,
                "should_stick_to_bottom": True,
                "web_view_loaded": True,
            },
        )
        report = build_live_preview_check(db, window="15m", provider="codex", surface="ios")

    cell = report.cells[0]
    assert cell.verdict == "ok"
    assert cell.coverage == "partial"
    assert cell.missing == ["ios_render_duration_ms"]
    assert cell.signals.ios_render_duration_events == 0


def test_live_preview_ios_render_duration_flows_into_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex")
        _record_render(
            db,
            session_id=session.id,
            surface="ios",
            latency_ms=240,
            webkit={
                "stage": "rendered",
                "payload_byte_size": 4096,
                "row_count": 18,
                "render_duration_ms": 42,
                "render_sequence": 7,
                "js_failure_count": 0,
                "should_stick_to_bottom": True,
                "web_view_loaded": True,
            },
        )
        report = build_live_preview_check(db, window="15m", provider="CODEX", surface="IOS")

    cell = report.cells[0]
    assert cell.verdict == "ok"
    assert cell.coverage == "full"
    assert cell.missing == []
    assert cell.signals.ios_render_duration_events == 1
    assert cell.signals.ios_render_duration_p95_ms == 42


def test_live_preview_slow_p95_is_degraded_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db, provider="codex")
        session_id = session.id
        _record_render(db, session_id=session.id, latency_ms=100, event_id="fast")
        _record_render(db, session_id=session.id, latency_ms=600, event_id="mid")
        _record_render(db, session_id=session.id, latency_ms=1000, event_id="slow")
        report = build_live_preview_check(db, window="15m", provider="codex", surface="web")

    cell = report.cells[0]
    assert cell.verdict == "degraded"
    assert cell.signals.render_p95_ms == 960
    assert cell.evidence_refs[0].id == str(session_id)
    assert cell.evidence_refs[0].latency_ms == 1000


def test_product_health_check_routes_expose_summary_and_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _seed_session(db, provider="codex")
        _record_render(db, session_id=session.id, latency_ms=100, event_id="a")

    client = _make_client(SessionLocal)
    try:
        summary = client.get("/observability/checks?window=15m&provider=codex")
        assert summary.status_code == 200
        summary_payload = summary.json()
        checks = {item["check"]: item for item in summary_payload["checks"]}
        assert checks["live_preview"]["verdict"] == "ok"
        assert checks["live_preview"]["coverage"] == "full"
        assert "within threshold" in checks["live_preview"]["headline"]

        detail = client.get("/observability/checks/live_preview?window=15m&provider=codex")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert detail_payload["check"] == "live_preview"
        assert detail_payload["cells"][0]["thresholds"]["render_p95_ms_ok"] == 500
        assert detail_payload["cells"][0]["signals"]["events"] == 1
    finally:
        api_app.dependency_overrides.clear()


def test_product_health_rejects_invalid_window(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client = _make_client(SessionLocal)
    try:
        response = client.get("/observability/checks/live_preview?window=forever")
        assert response.status_code == 400
        assert "Window must look like" in response.json()["detail"]
    finally:
        api_app.dependency_overrides.clear()
