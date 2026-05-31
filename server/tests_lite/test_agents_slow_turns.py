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
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentHeartbeat
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionTurn
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_DURABLE
from zerg.services.session_turns import SESSION_TURN_STATE_FAILED
from zerg.services.session_turns import SESSION_TURN_STATE_TERMINAL


def _make_db(tmp_path):
    db_path = tmp_path / "test_agents_slow_turns.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
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


_TRANSPORT_TO_CONTROL_PLANE = {
    "claude_channel_bridge": "claude_channel_bridge",
    "codex_app_server": "codex_app_server",
    "opencode_process": "opencode_process",
    "antigravity_hook_inbox": "antigravity_hook_inbox",
    "antigravity_process": "antigravity_process",
}


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
        started_at=datetime(2026, 4, 23, 18, 0, 0, tzinfo=timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.flush()

    # Session-identity-kernel cleanup: managed_transport derives from
    # session_connections.control_plane on the primary thread's latest run.
    # Seed the kernel rows so transport-derivation queries find the session.
    thread = SessionThread(
        id=uuid4(),
        session_id=session.id,
        provider=provider,
        is_primary=1,
    )
    db.add(thread)
    db.flush()
    session.primary_thread_id = thread.id

    if managed_transport is not None:
        control_plane = _TRANSPORT_TO_CONTROL_PLANE.get(managed_transport, managed_transport)
        run = SessionRun(
            id=uuid4(),
            thread_id=thread.id,
            provider=provider,
            host_id=device_id,
            started_at=datetime(2026, 4, 23, 18, 0, 0, tzinfo=timezone.utc),
        )
        db.add(run)
        db.flush()
        db.add(
            SessionConnection(
                run_id=run.id,
                control_plane=control_plane,
                acquisition_kind="spawned_control",
                state="attached",
            )
        )
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
            device_name="demo-machine",
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
        assert first["device_name"] == "demo-machine"
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

        degraded = client.get(
            "/agents/turns/slow"
            "?provider=claude"
            "&machine_status=degraded"
            "&hours_back=24"
            "&min_total_turn_time_ms=30000"
            "&stale_after_seconds=3600"
        )
        assert degraded.status_code == 200, degraded.text
        degraded_payload = degraded.json()
        assert degraded_payload["total"] == 1
        assert degraded_payload["turns"][0]["project"] == "hdr"
        assert degraded_payload["turns"][0]["machine"]["status"] == "degraded"
    finally:
        api_app_ref.dependency_overrides = {}


def test_turn_summary_route_returns_overall_and_provider_percentiles(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        claude_a = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="broken-machine",
            managed_transport="claude_channel_bridge",
        )
        claude_b = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="broken-machine",
            managed_transport="claude_channel_bridge",
        )
        codex = _seed_session(
            db,
            provider="codex",
            project="zerg",
            device_id="healthy-machine",
            managed_transport="codex_app_server",
        )

        _seed_turn(
            db,
            session_id=claude_a.id,
            request_id="req-claude-a",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(hours=2),
            send_accepted_at=pinned_now - timedelta(hours=2) + timedelta(seconds=1),
            active_phase_observed_at=pinned_now - timedelta(hours=2) + timedelta(seconds=5),
            terminal_at=pinned_now - timedelta(hours=2) + timedelta(seconds=70),
            durable_at=pinned_now - timedelta(hours=2) + timedelta(seconds=72),
        )
        _seed_turn(
            db,
            session_id=claude_b.id,
            request_id="req-claude-b",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=40),
            send_accepted_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=1),
            active_phase_observed_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=2),
            terminal_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=31),
            durable_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=32),
        )
        _seed_turn(
            db,
            session_id=codex.id,
            request_id="req-codex",
            state=SESSION_TURN_STATE_TERMINAL,
            user_submitted_at=pinned_now - timedelta(hours=1),
            send_accepted_at=pinned_now - timedelta(hours=1) + timedelta(seconds=1),
            active_phase_observed_at=pinned_now - timedelta(hours=1) + timedelta(seconds=3),
            terminal_at=pinned_now - timedelta(hours=1) + timedelta(seconds=45),
        )

        _seed_heartbeat(
            db,
            device_id="broken-machine",
            received_at=pinned_now - timedelta(minutes=2),
            spool_dead=1,
        )
        _seed_heartbeat(
            db,
            device_id="healthy-machine",
            received_at=pinned_now - timedelta(minutes=2),
        )

    client, api_app_ref = _make_client(SessionLocal)
    try:
        response = client.get("/agents/turns/summary?hours_back=24&slow_threshold_ms=30000&stale_after_seconds=3600")
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["hours_back"] == 24
        assert payload["slow_threshold_ms"] == 30000
        assert payload["summary"] == {
            "completed_turns": 3,
            "slow_turns": 3,
            "durable_turns": 2,
            "terminal_only_turns": 1,
            "submit_to_send_ms": {"p50": 1000, "p95": 1000, "max": 1000},
            "submit_to_active_ms": {"p50": 3000, "p95": 4800, "max": 5000},
            "submit_to_terminal_ms": {"p50": 45000, "p95": 67500, "max": 70000},
            "active_to_terminal_ms": {"p50": 42000, "p95": 62700, "max": 65000},
            "terminal_to_durable_ms": {"p50": 1500, "p95": 1950, "max": 2000},
            "total_turn_time_ms": {"p50": 45000, "p95": 69300, "max": 72000},
        }

        assert payload["providers"] == [
            {
                "provider": "claude",
                "completed_turns": 2,
                "slow_turns": 2,
                "durable_turns": 2,
                "terminal_only_turns": 0,
                "submit_to_send_ms": {"p50": 1000, "p95": 1000, "max": 1000},
                "submit_to_active_ms": {"p50": 3500, "p95": 4850, "max": 5000},
                "submit_to_terminal_ms": {"p50": 50500, "p95": 68050, "max": 70000},
                "active_to_terminal_ms": {"p50": 47000, "p95": 63200, "max": 65000},
                "terminal_to_durable_ms": {"p50": 1500, "p95": 1950, "max": 2000},
                "total_turn_time_ms": {"p50": 52000, "p95": 70000, "max": 72000},
            },
            {
                "provider": "codex",
                "completed_turns": 1,
                "slow_turns": 1,
                "durable_turns": 0,
                "terminal_only_turns": 1,
                "submit_to_send_ms": {"p50": 1000, "p95": 1000, "max": 1000},
                "submit_to_active_ms": {"p50": 3000, "p95": 3000, "max": 3000},
                "submit_to_terminal_ms": {"p50": 45000, "p95": 45000, "max": 45000},
                "active_to_terminal_ms": {"p50": 42000, "p95": 42000, "max": 42000},
                "terminal_to_durable_ms": {"p50": None, "p95": None, "max": None},
                "total_turn_time_ms": {"p50": 45000, "p95": 45000, "max": 45000},
            },
        ]

        higher_threshold = client.get(
            "/agents/turns/summary?hours_back=24&slow_threshold_ms=60000&stale_after_seconds=3600"
        )
        assert higher_threshold.status_code == 200, higher_threshold.text
        higher_payload = higher_threshold.json()
        assert higher_payload["summary"]["completed_turns"] == 3
        assert higher_payload["summary"]["slow_turns"] == 1
    finally:
        api_app_ref.dependency_overrides = {}


def test_turn_summary_route_respects_machine_status_and_state_filters(tmp_path, monkeypatch):
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
        broken_terminal = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="broken-machine",
            managed_transport="claude_channel_bridge",
        )
        missing_heartbeat = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="missing-heartbeat-machine",
            managed_transport="claude_channel_bridge",
        )
        other_project = _seed_session(
            db,
            provider="claude",
            project="hdr",
            device_id="broken-machine",
            managed_transport="claude_channel_bridge",
        )

        _seed_turn(
            db,
            session_id=broken_a.id,
            request_id="req-broken-a",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(hours=3),
            terminal_at=pinned_now - timedelta(hours=3) + timedelta(seconds=55),
            durable_at=pinned_now - timedelta(hours=3) + timedelta(seconds=60),
        )
        _seed_turn(
            db,
            session_id=broken_b.id,
            request_id="req-broken-b",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=30),
            terminal_at=pinned_now - timedelta(minutes=30) + timedelta(seconds=45),
            durable_at=pinned_now - timedelta(minutes=30) + timedelta(seconds=50),
        )
        _seed_turn(
            db,
            session_id=broken_terminal.id,
            request_id="req-broken-terminal",
            state=SESSION_TURN_STATE_TERMINAL,
            user_submitted_at=pinned_now - timedelta(minutes=80),
            terminal_at=pinned_now - timedelta(minutes=80) + timedelta(seconds=70),
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
        _seed_turn(
            db,
            session_id=other_project.id,
            request_id="req-other-project",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=20),
            terminal_at=pinned_now - timedelta(minutes=20) + timedelta(seconds=85),
            durable_at=pinned_now - timedelta(minutes=20) + timedelta(seconds=90),
        )

        _seed_heartbeat(
            db,
            device_id="broken-machine",
            received_at=pinned_now - timedelta(minutes=2),
            spool_dead=1,
        )

    client, api_app_ref = _make_client(SessionLocal)
    try:
        response = client.get(
            "/agents/turns/summary"
            "?provider=claude"
            "&project=zerg"
            "&state=durable"
            "&machine_status=broken"
            "&hours_back=24"
            "&slow_threshold_ms=40000"
            "&stale_after_seconds=3600"
        )
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["summary"] == {
            "completed_turns": 2,
            "slow_turns": 2,
            "durable_turns": 2,
            "terminal_only_turns": 0,
            "submit_to_send_ms": {"p50": None, "p95": None, "max": None},
            "submit_to_active_ms": {"p50": None, "p95": None, "max": None},
            "submit_to_terminal_ms": {"p50": 50000, "p95": 54500, "max": 55000},
            "active_to_terminal_ms": {"p50": None, "p95": None, "max": None},
            "terminal_to_durable_ms": {"p50": 5000, "p95": 5000, "max": 5000},
            "total_turn_time_ms": {"p50": 55000, "p95": 59500, "max": 60000},
        }
        assert payload["providers"] == [
            {
                "provider": "claude",
                "completed_turns": 2,
                "slow_turns": 2,
                "durable_turns": 2,
                "terminal_only_turns": 0,
                "submit_to_send_ms": {"p50": None, "p95": None, "max": None},
                "submit_to_active_ms": {"p50": None, "p95": None, "max": None},
                "submit_to_terminal_ms": {"p50": 50000, "p95": 54500, "max": 55000},
                "active_to_terminal_ms": {"p50": None, "p95": None, "max": None},
                "terminal_to_durable_ms": {"p50": 5000, "p95": 5000, "max": 5000},
                "total_turn_time_ms": {"p50": 55000, "p95": 59500, "max": 60000},
            }
        ]
    finally:
        api_app_ref.dependency_overrides = {}


def test_slow_turns_route_excludes_old_turns_and_preserves_total_for_overflow_offset(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        recent_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="recent-machine",
            managed_transport="claude_channel_bridge",
        )
        old_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="old-machine",
            managed_transport="claude_channel_bridge",
        )
        _seed_turn(
            db,
            session_id=recent_session.id,
            request_id="req-recent",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=30),
            terminal_at=pinned_now - timedelta(minutes=30) + timedelta(seconds=40),
            durable_at=pinned_now - timedelta(minutes=30) + timedelta(seconds=42),
        )
        _seed_turn(
            db,
            session_id=old_session.id,
            request_id="req-old",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(hours=3),
            terminal_at=pinned_now - timedelta(hours=3) + timedelta(seconds=80),
            durable_at=pinned_now - timedelta(hours=3) + timedelta(seconds=82),
        )

    client, api_app_ref = _make_client(SessionLocal)
    try:
        response = client.get("/agents/turns/slow?hours_back=1&min_total_turn_time_ms=30000&offset=5")
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["total"] == 1
        assert payload["turns"] == []
    finally:
        api_app_ref.dependency_overrides = {}


def test_slow_turns_route_supports_completed_failed_state_filter(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        failed_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="failed-machine",
            managed_transport="claude_channel_bridge",
        )
        durable_session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="durable-machine",
            managed_transport="claude_channel_bridge",
        )
        failed_turn = _seed_turn(
            db,
            session_id=failed_session.id,
            request_id="req-failed",
            state=SESSION_TURN_STATE_FAILED,
            user_submitted_at=pinned_now - timedelta(minutes=50),
            terminal_at=pinned_now - timedelta(minutes=50) + timedelta(seconds=39),
        )
        _seed_turn(
            db,
            session_id=durable_session.id,
            request_id="req-durable",
            state=SESSION_TURN_STATE_DURABLE,
            user_submitted_at=pinned_now - timedelta(minutes=40),
            terminal_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=55),
            durable_at=pinned_now - timedelta(minutes=40) + timedelta(seconds=60),
        )
        failed_turn_id = int(failed_turn.id)

    client, api_app_ref = _make_client(SessionLocal)
    try:
        response = client.get("/agents/turns/slow?hours_back=24&min_total_turn_time_ms=30000&state=failed")
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["total"] == 1
        assert [item["turn_id"] for item in payload["turns"]] == [failed_turn_id]
        assert payload["turns"][0]["state"] == SESSION_TURN_STATE_FAILED
        assert payload["turns"][0]["completed_at"] == "2026-04-23T20:10:39Z"
        assert payload["turns"][0]["timing"]["total_turn_time_ms"] == 39000
    finally:
        api_app_ref.dependency_overrides = {}


def test_turn_summary_route_materializes_managed_native_turns(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 21, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(session_turns_service, "utc_now", lambda: pinned_now)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

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
                    timestamp=pinned_now - timedelta(minutes=10),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="done",
                    timestamp=pinned_now - timedelta(minutes=10) + timedelta(seconds=16),
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

    client, api_app_ref = _make_client(SessionLocal)
    try:
        response = client.get("/agents/turns/summary?hours_back=24&slow_threshold_ms=30000&stale_after_seconds=3600")
        assert response.status_code == 200, response.text

        payload = response.json()
        assert payload["summary"]["completed_turns"] == 1
        assert payload["summary"]["durable_turns"] == 1
        assert payload["summary"]["total_turn_time_ms"] == {
            "p50": 16000,
            "p95": 16000,
            "max": 16000,
        }

        with SessionLocal() as verify_db:
            row = verify_db.query(SessionTurn).filter(SessionTurn.session_id == session_id).one()
            assert row.request_id.startswith("native:")
            assert row.state == SESSION_TURN_STATE_DURABLE
    finally:
        api_app_ref.dependency_overrides = {}
