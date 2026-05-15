"""Tests for the browser-facing observability routes."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.services.agent_heartbeat_health as machine_health_service
import zerg.services.observability_views as observability_views
import zerg.services.session_turns as session_turns_service
from zerg.database import get_db
from zerg.database import make_engine
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentHeartbeat
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurn
from zerg.services.session_turns import SESSION_TURN_STATE_DURABLE


def _make_db(tmp_path):
    db_path = tmp_path / "test_observability_routes.db"
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


def _seed_session(
    db,
    *,
    provider: str,
    project: str,
    device_id: str,
    managed_transport: str | None,
    device_name: str | None = None,
) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="test",
        project=project,
        device_id=device_id,
        device_name=device_name,
        provider_session_id=str(uuid4()),
        managed_transport=managed_transport,
        started_at=datetime(2026, 4, 23, 18, 0, 0, tzinfo=timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_turn(
    db,
    *,
    session_id,
    request_id: str,
    user_submitted_at: datetime,
    send_accepted_at: datetime | None = None,
    active_phase_observed_at: datetime | None = None,
    terminal_at: datetime | None = None,
    durable_at: datetime | None = None,
) -> SessionTurn:
    turn = SessionTurn(
        session_id=session_id,
        request_id=request_id,
        state=SESSION_TURN_STATE_DURABLE,
        user_submitted_at=user_submitted_at,
        send_accepted_at=send_accepted_at,
        active_phase_observed_at=active_phase_observed_at,
        terminal_at=terminal_at,
        durable_at=durable_at,
        created_at=user_submitted_at,
        updated_at=durable_at or terminal_at or send_accepted_at or user_submitted_at,
    )
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return turn


def _seed_heartbeat(
    db,
    *,
    device_id: str,
    received_at: datetime,
    version: str = "0.6.0",
    spool_dead: int = 0,
    consecutive_failures: int = 0,
) -> AgentHeartbeat:
    heartbeat = AgentHeartbeat(
        device_id=device_id,
        received_at=received_at,
        version=version,
        spool_dead=spool_dead,
        consecutive_failures=consecutive_failures,
        ship_attempts_1h=4,
        ship_successes_1h=4 if spool_dead == 0 and consecutive_failures == 0 else 2,
        disk_free_bytes=1_000,
        is_offline=0,
    )
    db.add(heartbeat)
    db.commit()
    db.refresh(heartbeat)
    return heartbeat


def test_browser_observability_routes_expose_overview_and_raw_slices(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(observability_views, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        broken_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="broken-machine",
            device_name="cube",
            managed_transport="claude_channel_bridge",
        )
        healthy_session = _seed_session(
            db,
            provider="codex",
            project="zerg",
            device_id="healthy-machine",
            device_name="laptop",
            managed_transport="codex_app_server",
        )
        unmanaged_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="ignored-machine",
            managed_transport=None,
        )

        _seed_turn(
            db,
            session_id=broken_session.id,
            request_id="req-slowest",
            user_submitted_at=pinned_now - timedelta(hours=2),
            send_accepted_at=pinned_now - timedelta(hours=2) + timedelta(seconds=1),
            active_phase_observed_at=pinned_now - timedelta(hours=2) + timedelta(seconds=5),
            terminal_at=pinned_now - timedelta(hours=2) + timedelta(seconds=70),
            durable_at=pinned_now - timedelta(hours=2) + timedelta(seconds=72),
        )
        _seed_turn(
            db,
            session_id=healthy_session.id,
            request_id="req-slower",
            user_submitted_at=pinned_now - timedelta(hours=1),
            send_accepted_at=pinned_now - timedelta(hours=1) + timedelta(seconds=1),
            active_phase_observed_at=pinned_now - timedelta(hours=1) + timedelta(seconds=3),
            terminal_at=pinned_now - timedelta(hours=1) + timedelta(seconds=44),
            durable_at=pinned_now - timedelta(hours=1) + timedelta(seconds=45),
        )
        _seed_turn(
            db,
            session_id=healthy_session.id,
            request_id="req-fast",
            user_submitted_at=pinned_now - timedelta(minutes=40),
            send_accepted_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=1),
            terminal_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=10),
            durable_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=12),
        )
        _seed_turn(
            db,
            session_id=unmanaged_session.id,
            request_id="req-unmanaged",
            user_submitted_at=pinned_now - timedelta(minutes=50),
            send_accepted_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=1),
            terminal_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=80),
            durable_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=81),
        )

        _seed_heartbeat(
            db,
            device_id="broken-machine",
            received_at=pinned_now - timedelta(minutes=2),
            spool_dead=1,
            consecutive_failures=1,
        )
        _seed_heartbeat(
            db,
            device_id="healthy-machine",
            received_at=pinned_now - timedelta(minutes=1),
        )
        _seed_heartbeat(
            db,
            device_id="ancient-machine",
            received_at=pinned_now - timedelta(days=14),
        )

    client = _make_client(SessionLocal)

    try:
        overview = client.get(
            "/observability/overview"
            "?hours_back=24"
            "&slow_threshold_ms=30000"
            "&stale_after_seconds=3600"
            "&machine_limit=2"
            "&slow_turn_limit=2"
        )
        assert overview.status_code == 200
        payload = overview.json()
        assert payload["generated_at"] == "2026-04-23T21:00:00Z"
        assert payload["summary"]["completed_turns"] == 3
        assert payload["summary"]["slow_turns"] == 2
        assert payload["machine_counts"] == {
            "total": 2,
            "healthy": 1,
            "degraded": 0,
            "offline": 0,
            "broken": 1,
        }
        assert {machine["device_id"] for machine in payload["machines"]} == {
            "broken-machine",
            "healthy-machine",
        }
        assert payload["machines"][0]["device_id"] == "broken-machine"
        assert payload["machines"][0]["status"] == "broken"
        assert payload["machines"][1]["device_id"] == "healthy-machine"
        assert payload["slow_turn_total"] == 2
        assert payload["slow_turns"][0]["request_id"] == "req-slowest"
        assert payload["slow_turns"][0]["machine"]["status"] == "broken"
        assert payload["providers"][0]["provider"] == "codex"
        assert payload["providers"][0]["completed_turns"] == 2
        assert payload["providers"][1]["provider"] == "claude"
        assert payload["providers"][1]["completed_turns"] == 1

        overview_wide_turn_window = client.get(
            "/observability/overview"
            "?hours_back=168"
            "&slow_threshold_ms=30000"
            "&stale_after_seconds=3600"
            "&machine_limit=4"
            "&slow_turn_limit=2"
        )
        assert overview_wide_turn_window.status_code == 200
        overview_wide_turn_window_payload = overview_wide_turn_window.json()
        assert overview_wide_turn_window_payload["machine_counts"]["total"] == 2
        assert "ancient-machine" not in {
            machine["device_id"] for machine in overview_wide_turn_window_payload["machines"]
        }

        summary = client.get(
            "/observability/turns/summary"
            "?provider=claude"
            "&hours_back=24"
            "&slow_threshold_ms=30000"
            "&stale_after_seconds=3600"
        )
        assert summary.status_code == 200
        summary_payload = summary.json()
        assert summary_payload["summary"]["completed_turns"] == 1
        assert summary_payload["summary"]["slow_turns"] == 1
        assert summary_payload["providers"][0]["provider"] == "claude"

        slow = client.get(
            "/observability/turns/slow"
            "?provider=claude"
            "&hours_back=24"
            "&min_total_turn_time_ms=30000"
            "&stale_after_seconds=3600"
        )
        assert slow.status_code == 200
        slow_payload = slow.json()
        assert slow_payload["total"] == 1
        assert slow_payload["turns"][0]["request_id"] == "req-slowest"

        broken = client.get("/observability/machines/health?status=broken&stale_after_seconds=3600")
        assert broken.status_code == 200
        broken_payload = broken.json()
        assert broken_payload["total"] == 1
        assert broken_payload["machines"][0]["device_id"] == "broken-machine"

        recent_default = client.get("/observability/machines/health?stale_after_seconds=3600")
        assert recent_default.status_code == 200
        recent_default_payload = recent_default.json()
        assert recent_default_payload["total"] == 2
        assert {machine["device_id"] for machine in recent_default_payload["machines"]} == {
            "broken-machine",
            "healthy-machine",
        }

        widened = client.get("/observability/machines/health?stale_after_seconds=3600&recent_within_hours=720")
        assert widened.status_code == 200
        widened_payload = widened.json()
        assert widened_payload["total"] == 3
        assert "ancient-machine" in {machine["device_id"] for machine in widened_payload["machines"]}
    finally:
        api_app.dependency_overrides.clear()


def test_browser_observability_overview_materializes_managed_native_turns(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(observability_views, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="cinder",
            managed_transport="claude_channel_bridge",
        )
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="continue",
                    timestamp=pinned_now - timedelta(minutes=12),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="done",
                    timestamp=pinned_now - timedelta(minutes=12) + timedelta(seconds=14),
                ),
            ]
        )
        db.commit()
        _seed_heartbeat(
            db,
            device_id="cinder",
            received_at=pinned_now - timedelta(minutes=1),
        )
        session_id = session.id

    client = _make_client(SessionLocal)

    try:
        overview = client.get(
            "/observability/overview"
            "?hours_back=24"
            "&slow_threshold_ms=30000"
            "&stale_after_seconds=3600"
            "&machine_limit=4"
            "&slow_turn_limit=4"
        )
        assert overview.status_code == 200, overview.text

        payload = overview.json()
        assert payload["summary"]["completed_turns"] == 1
        assert payload["summary"]["durable_turns"] == 1
        assert payload["summary"]["total_turn_time_ms"] == {
            "p50": 14000,
            "p95": 14000,
            "max": 14000,
        }

        with SessionLocal() as verify_db:
            row = verify_db.query(SessionTurn).filter(SessionTurn.session_id == session_id).one()
            assert row.request_id.startswith("native:")
            assert row.state == SESSION_TURN_STATE_DURABLE
    finally:
        api_app.dependency_overrides.clear()
