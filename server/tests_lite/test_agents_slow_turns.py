"""Tests for the cross-session slow managed turns endpoint."""

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
import zerg.services.session_turns as session_turns_service
from zerg.database import get_db
from zerg.database import make_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurn
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_DURABLE
from zerg.services.session_turns import SESSION_TURN_STATE_TERMINAL


def _make_db(tmp_path):
    db_path = tmp_path / "test_agents_slow_turns.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_client(SessionLocal):
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(device_id="slow-turns", id="token-1", owner_id=1)

    def override_require_single_tenant():
        return None

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = override_require_single_tenant
    client = TestClient(api_app)
    return client, api_app


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
    state: str,
    user_submitted_at: datetime,
    send_accepted_at: datetime | None = None,
    active_phase_observed_at: datetime | None = None,
    terminal_at: datetime | None = None,
    durable_at: datetime | None = None,
) -> SessionTurn:
    turn = SessionTurn(
        session_id=session_id,
        request_id=request_id,
        state=state,
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
    is_offline: int = 0,
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
        is_offline=is_offline,
    )
    db.add(heartbeat)
    db.commit()
    db.refresh(heartbeat)
    return heartbeat


def test_slow_turns_route_returns_managed_completed_turns_with_machine_health(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

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
        missing_heartbeat_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="no-heartbeat-machine",
            managed_transport="claude_channel_bridge",
        )
        active_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="active-machine",
            managed_transport="claude_channel_bridge",
        )

        slowest = _seed_turn(
            db,
            session_id=broken_session.id,
            request_id="req-slowest",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(hours=2),
            send_accepted_at=pinned_now - timedelta(hours=2) + timedelta(seconds=1),
            active_phase_observed_at=pinned_now - timedelta(hours=2) + timedelta(seconds=5),
            terminal_at=pinned_now - timedelta(hours=2) + timedelta(seconds=70),
            durable_at=pinned_now - timedelta(hours=2) + timedelta(seconds=72),
        )
        slower = _seed_turn(
            db,
            session_id=healthy_session.id,
            request_id="req-slower",
            state=SESSION_TURN_STATE_DURABLE,
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
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=40),
            send_accepted_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=1),
            terminal_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=10),
            durable_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=12),
        )
        _seed_turn(
            db,
            session_id=unmanaged_session.id,
            request_id="req-unmanaged",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=50),
            send_accepted_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=1),
            terminal_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=80),
            durable_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=81),
        )
        _seed_turn(
            db,
            session_id=active_session.id,
            request_id="req-active",
            state=SESSION_TURN_STATE_ACTIVE,
            user_submitted_at=pinned_now - timedelta(minutes=15),
            send_accepted_at=pinned_now - timedelta(minutes=15) + timedelta(seconds=1),
            active_phase_observed_at=pinned_now - timedelta(minutes=15) + timedelta(seconds=5),
        )
        no_heartbeat = _seed_turn(
            db,
            session_id=missing_heartbeat_session.id,
            request_id="req-no-heartbeat",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=35),
            send_accepted_at=pinned_now - timedelta(minutes=35) + timedelta(seconds=1),
            terminal_at=pinned_now - timedelta(minutes=35) + timedelta(seconds=31),
            durable_at=pinned_now - timedelta(minutes=35) + timedelta(seconds=32),
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
            received_at=pinned_now - timedelta(minutes=3),
        )
        broken_session_id = str(broken_session.id)
        healthy_session_id = str(healthy_session.id)
        slowest_turn_id = int(slowest.id)
        slower_turn_id = int(slower.id)
        no_heartbeat_turn_id = int(no_heartbeat.id)

    client, api_app_ref = _make_client(SessionLocal)
    try:
        response = client.get("/agents/turns/slow?hours_back=24&min_total_turn_time_ms=30000&stale_after_seconds=3600")
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["total"] == 3
        assert payload["min_total_turn_time_ms"] == 30000
        assert [item["turn_id"] for item in payload["turns"]] == [slowest_turn_id, slower_turn_id, no_heartbeat_turn_id]

        first = payload["turns"][0]
        assert first["session_id"] == broken_session_id
        assert first["provider"] == "claude"
        assert first["project"] == "zerg"
        assert first["device_id"] == "broken-machine"
        assert first["device_name"] == "cube"
        assert first["managed_transport"] == "claude_channel_bridge"
        assert first["total_turn_time_ms"] == 72000
        assert first["completed_at"] == "2026-04-23T19:01:12Z"
        assert first["timing"] == {
            "submit_to_send_ms": 1000,
            "submit_to_active_ms": 5000,
            "submit_to_terminal_ms": 70000,
            "active_to_terminal_ms": 65000,
            "terminal_to_durable_ms": 2000,
            "total_turn_time_ms": 72000,
        }
        assert first["machine"] == {
            "device_id": "broken-machine",
            "status": "broken",
            "status_reason": "spool_dead",
            "status_summary": "1 dead-letter range(s) need repair.",
            "last_heartbeat_at": "2026-04-23T20:58:00Z",
            "heartbeat_age_seconds": 120,
            "is_stale": False,
            "version": "0.6.0",
        }

        second = payload["turns"][1]
        assert second["session_id"] == healthy_session_id
        assert second["provider"] == "codex"
        assert second["total_turn_time_ms"] == 45000
        assert second["machine"]["status"] == "healthy"

        third = payload["turns"][2]
        assert third["device_id"] == "no-heartbeat-machine"
        assert third["total_turn_time_ms"] == 32000
        assert third["machine"] is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_slow_turns_route_supports_filters_machine_status_and_pagination(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        broken_a = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="broken-machine",
            managed_transport="claude_channel_bridge",
        )
        broken_b = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="broken-machine",
            managed_transport="claude_channel_bridge",
        )
        degraded = _seed_session(
            db,
            provider="claude",
            project="hdr",
            device_id="degraded-machine",
            managed_transport="claude_channel_bridge",
        )
        missing_heartbeat = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="missing-heartbeat-machine",
            managed_transport="claude_channel_bridge",
        )

        fastest_broken = _seed_turn(
            db,
            session_id=broken_b.id,
            request_id="req-broken-fast",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=30),
            terminal_at=pinned_now - timedelta(minutes=30) + timedelta(seconds=45),
            durable_at=pinned_now - timedelta(minutes=30) + timedelta(seconds=50),
        )
        _seed_turn(
            db,
            session_id=broken_a.id,
            request_id="req-broken-slow",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(hours=3),
            terminal_at=pinned_now - timedelta(hours=3) + timedelta(seconds=85),
            durable_at=pinned_now - timedelta(hours=3) + timedelta(seconds=90),
        )
        _seed_turn(
            db,
            session_id=broken_a.id,
            request_id="req-broken-terminal",
            state=SESSION_TURN_STATE_TERMINAL,
            user_submitted_at=pinned_now - timedelta(minutes=80),
            terminal_at=pinned_now - timedelta(minutes=80) + timedelta(seconds=70),
        )
        _seed_turn(
            db,
            session_id=degraded.id,
            request_id="req-degraded",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=50),
            terminal_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=55),
            durable_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=60),
        )
        _seed_turn(
            db,
            session_id=missing_heartbeat.id,
            request_id="req-missing-heartbeat",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=65),
            terminal_at=pinned_now - timedelta(minutes=65) + timedelta(seconds=70),
            durable_at=pinned_now - timedelta(minutes=65) + timedelta(seconds=75),
        )

        _seed_heartbeat(
            db,
            device_id="broken-machine",
            received_at=pinned_now - timedelta(minutes=2),
            spool_dead=1,
        )
        _seed_heartbeat(
            db,
            device_id="degraded-machine",
            received_at=pinned_now - timedelta(minutes=2),
            consecutive_failures=2,
        )
        fastest_broken_turn_id = int(fastest_broken.id)

    client, api_app_ref = _make_client(SessionLocal)
    try:
        response = client.get(
            "/agents/turns/slow"
            "?provider=claude"
            "&project=zerg"
            "&state=durable"
            "&machine_status=broken"
            "&hours_back=24"
            "&min_total_turn_time_ms=30000"
            "&stale_after_seconds=3600"
            "&limit=1"
            "&offset=1"
        )
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["total"] == 2
        assert len(payload["turns"]) == 1
        item = payload["turns"][0]
        assert item["turn_id"] == fastest_broken_turn_id
        assert item["provider"] == "claude"
        assert item["project"] == "zerg"
        assert item["machine"]["status"] == "broken"
        assert item["total_turn_time_ms"] == 50000
    finally:
        api_app_ref.dependency_overrides = {}
