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
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurn
from zerg.services.session_observations import OBS_KIND_CLIENT_RENDER
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_observations import OBS_KIND_SERVER_FANOUT
from zerg.services.session_observations import SOURCE_DOMAIN_CLIENT
from zerg.services.session_observations import SOURCE_DOMAIN_RUNTIME
from zerg.services.session_observations import SOURCE_DOMAIN_SERVER
from zerg.services.session_observations import SOURCE_DOMAIN_TRANSCRIPT
from zerg.services.session_observations import record_session_observation
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
        managed_transport=managed_transport,
        started_at=datetime(2026, 4, 23, 18, 0, 0, tzinfo=timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.flush()

    # Session-identity-kernel cleanup: managed_transport derives from
    # session_connections.control_plane. Seed the kernel rows so the
    # observability endpoint sees this as managed.
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.models.agents import SessionThread

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
        plane_map = {
            "claude_channel_bridge": "claude_channel_bridge",
            "codex_app_server": "codex_app_server",
            "opencode_process": "opencode_process",
            "antigravity_process": "antigravity_process",
        }
        control_plane = plane_map.get(managed_transport, managed_transport)
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


def _dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


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


def test_session_latency_report_stitches_existing_evidence(tmp_path):
    SessionLocal = _make_db(tmp_path)

    provider_at = _dt_from_ms(1_779_391_436_648)
    engine_observed_at = _dt_from_ms(1_779_391_437_009)
    engine_enqueued_at = _dt_from_ms(1_779_391_437_374)
    job_started_at = _dt_from_ms(1_779_391_460_256)
    http_send_at = _dt_from_ms(1_779_391_461_394)
    server_handler_at = _dt_from_ms(1_779_391_461_425)
    server_store_at = _dt_from_ms(1_779_391_461_541)
    server_fanout_at = _dt_from_ms(1_779_391_461_600)
    client_received_at = _dt_from_ms(1_779_391_462_100)
    client_rendered_at = _dt_from_ms(1_779_391_463_231)
    client_clock_skew_ms = 1_200

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="claude",
            project="zerg",
            device_id="cinder",
            managed_transport="claude_channel_bridge",
        )
        user_event = AgentEvent(
            session_id=session.id,
            role="user",
            content_text="do not leak this in observability",
            timestamp=provider_at,
            source_path="/tmp/claude-session.jsonl",
            source_offset=1_506_270,
            event_uuid="user-event-uuid",
            event_hash="hash-user",
        )
        system_event = AgentEvent(
            session_id=session.id,
            role="system",
            content_text="snapshot",
            timestamp=_dt_from_ms(1_779_391_436_894),
            source_path="/tmp/claude-session.jsonl",
            source_offset=1_510_052,
            event_uuid="system-event-uuid",
            event_hash="hash-system",
        )
        db.add_all([user_event, system_event])
        db.commit()
        db.refresh(user_event)
        db.refresh(system_event)

        record_session_observation(
            db,
            observation_id="provider_event:user-event-uuid",
            session_id=session.id,
            runtime_key=None,
            provider="claude",
            device_id="cinder",
            source_domain=SOURCE_DOMAIN_TRANSCRIPT,
            source="claude_transcript",
            kind=OBS_KIND_PROVIDER_EVENT,
            source_path="/tmp/claude-session.jsonl",
            source_offset=1_506_270,
            source_cursor="user-event-uuid",
            observed_at=provider_at,
            received_at=_dt_from_ms(1_779_391_461_450),
            payload={
                "role": "user",
                "timestamp": provider_at.isoformat(),
                "event_uuid": "user-event-uuid",
            },
        )
        trace_id = f"{session.id}:1506270:1510998:1779391461394"
        record_session_observation(
            db,
            observation_id=f"runtime:ship_trace:{trace_id}",
            session_id=session.id,
            runtime_key=f"claude:{session.id}",
            provider="claude",
            device_id="cinder",
            source_domain=SOURCE_DOMAIN_RUNTIME,
            source="agents_ingest_trace",
            kind=OBS_KIND_RUNTIME_SIGNAL,
            source_cursor=f"binding_signal:ship_trace:{trace_id}",
            observed_at=server_store_at,
            received_at=server_store_at,
            payload={
                "kind": "binding_signal",
                "phase": None,
                "tool_name": None,
                "freshness_ms": None,
                "dedupe_key": f"ship_trace:{session.id}:{trace_id}",
                "payload": {
                    "progress_kind": "ship_pipeline_trace",
                    "ship_trace": {
                        "schema": "ship_trace.v1",
                        "trace_id": trace_id,
                        "provider": "claude",
                        "session_id": str(session.id),
                        "path": "/tmp/claude-session.jsonl",
                        "work_context": "live_transcript",
                        "observation_source": "fsevent",
                        "event_count": 2,
                        "offset": 1_506_270,
                        "new_offset": 1_510_998,
                        "range_bytes": 4_728,
                        "observed_at_ms": int(engine_observed_at.timestamp() * 1000),
                        "enqueued_at_ms": int(engine_enqueued_at.timestamp() * 1000),
                        "job_started_at_ms": int(job_started_at.timestamp() * 1000),
                        "http_send_started_at_ms": int(http_send_at.timestamp() * 1000),
                        "observation_to_enqueue_ms": 365,
                        "enqueue_to_job_ms": 22_882,
                        "job_to_http_ms": 1_138,
                    },
                    "server_trace": {
                        "handler_entered_at_ms": int(server_handler_at.timestamp() * 1000),
                        "store_returned_at_ms": int(server_store_at.timestamp() * 1000),
                        "store_write_ms": 116,
                    },
                },
            },
        )
        record_session_observation(
            db,
            observation_id=f"server_fanout:{session.id}:{trace_id}",
            session_id=session.id,
            runtime_key=None,
            provider="claude",
            device_id="cinder",
            source_domain=SOURCE_DOMAIN_SERVER,
            source="session_pubsub",
            kind=OBS_KIND_SERVER_FANOUT,
            source_cursor=f"trace:{trace_id}",
            observed_at=server_fanout_at,
            received_at=server_fanout_at + timedelta(milliseconds=4),
            payload={
                "kind": "ingest",
                "session_id": str(session.id),
                "events_inserted": 2,
                "provider": "claude",
                "latest_event_id": system_event.id,
                "server_fanout_at_ms": int(server_fanout_at.timestamp() * 1000),
                "ship_trace_id": trace_id,
                "session_pubsub_seq": 17,
                "timeline_pubsub_seq": 29,
            },
        )
        record_session_observation(
            db,
            observation_id=f"client_render:ios:{session.id}:{system_event.id}:1779391463231",
            session_id=session.id,
            runtime_key=None,
            provider="claude",
            device_id="cinder",
            source_domain=SOURCE_DOMAIN_CLIENT,
            source="client_render_beacon",
            kind=OBS_KIND_CLIENT_RENDER,
            source_cursor=f"event:{system_event.id}",
            observed_at=client_rendered_at,
            received_at=client_rendered_at + timedelta(milliseconds=144),
            payload={
                "event_id": str(system_event.id),
                "surface": "ios",
                "managed": True,
                "emitted_at_ms": int(system_event.timestamp.timestamp() * 1000),
                "rendered_at_ms": int(client_rendered_at.timestamp() * 1000) + client_clock_skew_ms,
                "clock_skew_ms": client_clock_skew_ms,
                "server_fanout_at_ms": int(server_fanout_at.timestamp() * 1000),
                "client_received_at_ms": int(client_received_at.timestamp() * 1000) + client_clock_skew_ms,
                "pubsub_seq": 17,
                "latency_ms": 26_337,
                "webkit": {
                    "stage": "rendered",
                    "latest_item_id": f"user:{user_event.id}",
                },
            },
        )
        db.commit()
        session_id = session.id
        user_event_id = user_event.id

    client = _make_client(SessionLocal)
    try:
        response = client.get(f"/observability/sessions/{session_id}/latency?event_limit=5&surface=ios")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["session"]["session_id"] == str(session_id)

        event = next(item for item in payload["events"] if item["event_id"] == user_event_id)
        assert "do not leak" not in response.text
        assert event["ship_trace"]["trace_id"] == trace_id
        assert event["server_fanout"]["ship_trace_id"] == trace_id
        assert event["first_client_render"]["matched_by"] == "latest_item_id"
        assert event["total_provider_to_first_render_ms"] == 26_583
        assert event["measured_total_ms"] == 26_583
        assert event["unaccounted_ms"] == 0
        assert event["client_clock_skew_ms"] == client_clock_skew_ms
        assert event["bottleneck"] == {
            "stage_key": "engine_enqueued_to_job_started",
            "label": "Engine enqueued -> job started",
            "duration_ms": 22_882,
        }
        stages = {stage["key"]: stage for stage in event["stages"]}
        assert stages["provider_to_engine_observed"]["duration_ms"] == 361
        assert stages["provider_to_engine_observed"]["confidence"] == "derived"
        assert stages["engine_observed_to_enqueued"]["duration_ms"] == 365
        assert stages["engine_enqueued_to_job_started"]["duration_ms"] == 22_882
        assert stages["http_send_to_server_handler"]["confidence"] == "derived"
        assert stages["server_handler_to_store_returned"]["duration_ms"] == 116
        assert stages["server_store_to_fanout"]["duration_ms"] == 59
        assert stages["server_fanout_to_client_received"]["duration_ms"] == 500
        assert stages["server_fanout_to_client_received"]["confidence"] == "derived"
        assert stages["client_received_to_rendered"]["duration_ms"] == 1_131
        assert stages["client_received_to_rendered"]["confidence"] == "derived"
        assert payload["known_unimplemented_probes"] == []
    finally:
        api_app.dependency_overrides.clear()


def test_session_latency_report_404s_for_unknown_session(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client = _make_client(SessionLocal)
    try:
        response = client.get(f"/observability/sessions/{uuid4()}/latency")
        assert response.status_code == 404
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
