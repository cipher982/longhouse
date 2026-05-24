"""Tests for the agent heartbeat ingest endpoint.

Covers:
- POST /agents/heartbeat creates a new AgentHeartbeat row
- Subsequent POST updates (inserts another row) for the same device
- Prune: rows older than 30 days are removed for that device
- Auth: missing token falls back to client IP

Uses in-memory SQLite. HTTP-level tests use TestClient with dependency_overrides
targeting api_app. No shared conftest.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import make_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRuntimeState
from zerg.database import Base
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import runtime_key_for_session

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_heartbeat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def _make_client(SessionLocal):
    """Create TestClient with get_db override + explicit machine auth."""
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(device_id="testclient", id="token-1")

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token

    client = TestClient(app, backend="asyncio")
    return client, api_app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_heartbeat_endpoint_creates_row(tmp_path):
    """POST /agents/heartbeat inserts a new AgentHeartbeat row."""
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.5.0",
                "daemon_pid": 12345,
                "spool_pending_count": 3,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 50_000_000_000,
                "is_offline": False,
            },
        )
        assert response.status_code == 204, f"Expected 204, got {response.status_code}: {response.text}"

        with SessionLocal() as db:
            rows = db.query(AgentHeartbeat).all()
            assert len(rows) == 1
            hb = rows[0]
            assert hb.version == "0.5.0"
            assert hb.last_ship_attempt_at is None
            assert hb.last_ship_result is None
            assert hb.last_ship_latency_ms is None
            assert hb.last_ship_http_status is None
            assert hb.spool_pending == 3
            assert hb.spool_dead == 0
            assert hb.ship_attempts_1h == 0
            assert hb.ship_successes_1h == 0
            assert hb.ship_rate_limited_1h == 0
            assert hb.ship_server_errors_1h == 0
            assert hb.ship_payload_rejections_1h == 0
            assert hb.ship_payload_too_large_1h == 0
            assert hb.ship_retryable_client_errors_1h == 0
            assert hb.ship_connect_errors_1h == 0
            assert hb.ship_latency_p50_ms_1h is None
            assert hb.ship_latency_p95_ms_1h is None
            assert hb.disk_free_bytes == 50_000_000_000
            assert hb.is_offline == 0
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_endpoint_appends_history_rows(tmp_path):
    """Two POSTs to /agents/heartbeat append two rows for the same device."""
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        for i in range(2):
            response = client.post(
                "/api/agents/heartbeat",
                json={
                    "version": "0.5.0",
                    "daemon_pid": 99,
                    "spool_pending_count": i,
                    "parse_error_count_1h": 0,
                    "consecutive_ship_failures": 0,
                    "disk_free_bytes": 1_000_000,
                    "is_offline": False,
                },
            )
            assert response.status_code == 204

        with SessionLocal() as db:
            count = db.query(AgentHeartbeat).count()
            assert count == 2, "Two heartbeats should produce two rows"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_prunes_old_rows(tmp_path):
    """Rows older than 30 days for the same device are pruned on next heartbeat."""
    SessionLocal = _make_db(tmp_path)

    # Insert an old row directly
    old_ts = datetime.now(timezone.utc) - timedelta(days=31)
    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="testclient",  # matches fallback IP in test
                received_at=old_ts,
                version="0.4.0",
                spool_pending=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=0,
                is_offline=0,
            )
        )
        db.commit()
        assert db.query(AgentHeartbeat).count() == 1

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.5.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
            },
        )
        assert response.status_code == 204

        with SessionLocal() as db:
            rows = db.query(AgentHeartbeat).all()
            # Old row should be pruned; new row remains
            assert len(rows) == 1, f"Expected 1 row after prune, got {len(rows)}"
            assert rows[0].version == "0.5.0"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_endpoint_persists_transport_summary_fields(tmp_path):
    """Heartbeat raw_json preserves the engine ship telemetry payload."""
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.5.0",
                "daemon_pid": 42,
                "last_ship_attempt_at": "2026-04-23T20:00:03Z",
                "last_ship_result": "rate_limited",
                "last_ship_latency_ms": 187,
                "last_ship_http_status": 429,
                "last_ship_error_kind": "rate_limited",
                "last_ship_error_message": "429: rate limited",
                "spool_pending_count": 7,
                "spool_dead_count": 2,
                "parse_error_count_1h": 2,
                "consecutive_ship_failures": 1,
                "ship_attempts_1h": 12,
                "ship_successes_1h": 8,
                "ship_rate_limited_1h": 3,
                "ship_server_errors_1h": 1,
                "ship_payload_rejections_1h": 0,
                "ship_payload_too_large_1h": 0,
                "ship_retryable_client_errors_1h": 0,
                "ship_connect_errors_1h": 0,
                "ship_latency_p50_ms_1h": 140,
                "ship_latency_p95_ms_1h": 260,
                "disk_free_bytes": 50_000_000,
                "is_offline": False,
            },
        )
        assert response.status_code == 204

        with SessionLocal() as db:
            row = db.query(AgentHeartbeat).one()
            raw = json.loads(row.raw_json)
            assert row.last_ship_attempt_at is not None
            # SQLite drops timezone info on round-trip; Postgres preserves UTC.
            assert row.last_ship_attempt_at.replace(tzinfo=None) == datetime(2026, 4, 23, 20, 0, 3)
            assert row.last_ship_result == "rate_limited"
            assert row.last_ship_latency_ms == 187
            assert row.last_ship_http_status == 429
            assert row.spool_dead == 2
            assert row.ship_attempts_1h == 12
            assert row.ship_successes_1h == 8
            assert row.ship_rate_limited_1h == 3
            assert row.ship_server_errors_1h == 1
            assert row.ship_payload_rejections_1h == 0
            assert row.ship_payload_too_large_1h == 0
            assert row.ship_retryable_client_errors_1h == 0
            assert row.ship_connect_errors_1h == 0
            assert row.ship_latency_p50_ms_1h == 140
            assert row.ship_latency_p95_ms_1h == 260
            assert raw["last_ship_attempt_at"] == "2026-04-23T20:00:03Z"
            assert raw["last_ship_result"] == "rate_limited"
            assert raw["last_ship_latency_ms"] == 187
            assert raw["last_ship_http_status"] == 429
            assert raw["last_ship_error_kind"] == "rate_limited"
            assert raw["last_ship_error_message"] == "429: rate limited"
            assert raw["spool_dead_count"] == 2
            assert raw["ship_attempts_1h"] == 12
            assert raw["ship_successes_1h"] == 8
            assert raw["ship_rate_limited_1h"] == 3
            assert raw["ship_latency_p50_ms_1h"] == 140
            assert raw["ship_latency_p95_ms_1h"] == 260
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_resolved_sessions_materialize_managed_control(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="codex",
                    environment="laptop",
                    started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                    last_activity_at=datetime(2026, 5, 5, 11, 59, tzinfo=timezone.utc),
                    provider_session_id="thread-codex",
                    execution_home="managed_local",
                    managed_transport="codex_app_server",
                    user_messages=1,
                    assistant_messages=1,
                    tool_calls=0,
                    is_writable_head=1,
                )
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "provider_session_id": "thread-codex",
                        "control_path": "managed",
                        "presentation_state": "managed_attached",
                        "state": "attached",
                        "phase": "thinking",
                        "tool_name": None,
                        "phase_observed_at": "2026-05-05T11:59:58Z",
                        "last_activity_at": "2026-05-05T11:59:58Z",
                        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                        "process": {"pid": 4201, "started_at": "2026-05-05T11:20:00Z"},
                        "bridge": {
                            "bridge_pid": 4202,
                            "app_server_pid": 4203,
                            "heartbeat_at": "2026-05-05T11:59:58Z",
                            "status": "ready",
                            "thread_subscription_status": "subscribed",
                        },
                        "evidence": {"process_observed": True, "transcript_observed": True},
                        "reason_codes": [],
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            row = db.query(AgentHeartbeat).one()
            raw = json.loads(row.raw_json)

            assert connection.control_plane == "codex_bridge"
            assert connection.state == "attached"
            assert connection.device_id == "testclient"
            assert connection.last_health_at is not None
            assert raw["sessions"][0]["control_path"] == "managed"
            assert raw["managed_sessions"] == []
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_legacy_managed_sessions_still_materialize_control(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="claude",
                    environment="laptop",
                    started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                    provider_session_id="thread-claude",
                    execution_home="managed_local",
                    managed_transport="claude_channel_bridge",
                    user_messages=1,
                    assistant_messages=1,
                    tool_calls=0,
                    is_writable_head=1,
                )
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 42,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "claude",
                        "machine_id": "cinder",
                        "sequence": 42,
                        "state": "attached",
                        "phase": "idle",
                        "bridge_status": "ready",
                        "observed_at": "2026-05-05T11:59:58Z",
                        "lease_ttl_ms": 900000,
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.control_plane == "claude_channel_bridge"
            assert connection.state == "attached"
            assert connection.device_id == "testclient"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_rejects_null_resolved_sessions(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": None,
            },
        )

        assert response.status_code == 422
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_detaches_missing_managed_control(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                provider_session_id="thread-codex",
                execution_home="managed_local",
                managed_transport="codex_app_server",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            connection.device_id = "testclient"
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.state == "detached"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_does_not_detach_other_device_control(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    first_session_id = uuid4()
    other_session_id = uuid4()

    try:
        with SessionLocal() as db:
            first_session = AgentSession(
                id=first_session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                provider_session_id="thread-codex-a",
                execution_home="managed_local",
                managed_transport="codex_app_server",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                is_writable_head=1,
            )
            other_session = AgentSession(
                id=other_session_id,
                provider="codex",
                environment="desktop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                provider_session_id="thread-codex-b",
                execution_home="managed_local",
                managed_transport="codex_app_server",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add_all([first_session, other_session])
            db.flush()
            _thread, _run, first_connection = seed_managed_kernel_rows(
                db, first_session, control_plane="codex_bridge"
            )
            first_connection.device_id = "testclient"
            _thread, _run, other_connection = seed_managed_kernel_rows(
                db, other_session, control_plane="codex_bridge"
            )
            other_connection.device_id = "other-device"
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            states_by_device = {row.device_id: row.state for row in db.query(SessionConnection).all()}
            assert states_by_device == {
                "testclient": "detached",
                "other-device": "attached",
            }
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_does_not_detach_unknown_device_control(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                provider_session_id="thread-codex",
                execution_home="managed_local",
                managed_transport="codex_app_server",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add(session)
            db.flush()
            seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.device_id is None
            assert connection.state == "attached"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_missing_managed_detach_can_be_disabled(monkeypatch, tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    monkeypatch.setenv("LONGHOUSE_DISABLE_MISSING_MANAGED_LEASE_DETACH", "1")
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                provider_session_id="thread-codex",
                execution_home="managed_local",
                managed_transport="codex_app_server",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            connection.device_id = "testclient"
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.device_id == "testclient"
            assert connection.state == "attached"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_closes_stale_unmanaged_session(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()
    provider_session_id = str(session_id)
    now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=now - timedelta(minutes=20),
                last_activity_at=now - timedelta(minutes=10),
                provider_session_id=provider_session_id,
                execution_home="unmanaged_local",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add(session)
            db.commit()
            ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key_for_session("codex", provider_session_id),
                        session_id=session_id,
                        provider="codex",
                        device_id="testclient",
                        source="codex_hook",
                        kind="phase_signal",
                        phase="thinking",
                        occurred_at=now - timedelta(minutes=10),
                        freshness_ms=90 * 1000,
                        dedupe_key="resolved-snapshot-unmanaged-phase",
                        payload={},
                    )
                ],
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
            assert state.terminal_state == "process_gone"
            assert state.terminal_source == "engine_process_snapshot"
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.skip(reason="UnmanagedSessionBinding removed; replacement uses kernel SessionConnection")
def test_heartbeat_accepts_unmanaged_session_bindings(tmp_path):
    """Phase 5 of session-liveness-honesty: machine agent may ship a list of
    unmanaged session bindings alongside the heartbeat. Upsert stores one
    row per (machine_id, provider, provider_session_id), and re-posting
    the same identity updates the existing row rather than duplicating.
    """
    from zerg.models.agents import UnmanagedSessionBinding

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        # First heartbeat: one binding, pid=1234, offset=100
        first = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
                "unmanaged_session_bindings": [
                    {
                        "machine_id": "cinder",
                        "provider": "codex",
                        "provider_session_id": "sess-abc",
                        "source_path": "/Users/x/.codex/sessions/sess-abc.jsonl",
                        "source_inode": 42,
                        "source_device": 99,
                        "pid": 1234,
                        "process_start_time": "2026-04-27T10:00:00Z",
                        "cwd": "/Users/x/repo",
                        "source_offset": 100,
                        "source_mtime": "2026-04-27T10:05:00Z",
                        "observed_at": "2026-04-27T10:05:00Z",
                    }
                ],
            },
        )
        assert first.status_code == 204, first.text

        with SessionLocal() as db:
            rows = db.query(UnmanagedSessionBinding).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.machine_id == "cinder"
            assert row.provider == "codex"
            assert row.provider_session_id == "sess-abc"
            assert row.pid == 1234
            assert row.source_offset == 100
            assert row.binding_state == "observed"

        # Second heartbeat: same identity, newer pid (process restarted) and offset.
        second = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
                "unmanaged_session_bindings": [
                    {
                        "machine_id": "cinder",
                        "provider": "codex",
                        "provider_session_id": "sess-abc",
                        "pid": 5678,
                        "process_start_time": "2026-04-27T11:00:00Z",
                        "source_offset": 250,
                        "observed_at": "2026-04-27T11:00:01Z",
                    }
                ],
            },
        )
        assert second.status_code == 204, second.text

        with SessionLocal() as db:
            rows = db.query(UnmanagedSessionBinding).all()
            assert len(rows) == 1, "Re-posted identity must upsert, not duplicate"
            row = rows[0]
            assert row.pid == 5678
            assert row.source_offset == 250
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.skip(reason="UnmanagedSessionBinding removed; replacement uses kernel SessionConnection")
def test_heartbeat_marks_missing_unmanaged_binding_stale(tmp_path):
    """An explicit empty unmanaged binding snapshot means prior local
    bindings from that device are gone, not still waiting on the user."""
    from zerg.models.agents import UnmanagedSessionBinding
    from zerg.services.unmanaged_bindings import load_binding_overlay

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    provider_session_id = "sess-gone"
    observed_at = datetime(2026, 4, 27, 10, 5, tzinfo=timezone.utc)

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=uuid4(),
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
                last_activity_at=observed_at,
                provider_session_id=provider_session_id,
                thread_root_session_id=None,
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add(session)
            db.commit()
            session_id = session.id

        first = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
                "unmanaged_session_bindings": [
                    {
                        "machine_id": "cinder",
                        "provider": "codex",
                        "provider_session_id": provider_session_id,
                        "source_path": f"/Users/x/.codex/sessions/{provider_session_id}.jsonl",
                        "pid": 1234,
                        "process_start_time": "2026-04-27T10:00:00Z",
                        "source_offset": 100,
                        "source_mtime": "2026-04-27T10:05:00Z",
                        "observed_at": "2026-04-27T10:05:00Z",
                    }
                ],
            },
        )
        assert first.status_code == 204, first.text

        second = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
                "unmanaged_session_bindings": [],
            },
        )
        assert second.status_code == 204, second.text

        with SessionLocal() as db:
            row = db.query(UnmanagedSessionBinding).one()
            assert row.session_id == session_id
            assert row.binding_state == "stale"

            overlay = load_binding_overlay(db, [session_id], now=datetime.now(timezone.utc))
            assert overlay[session_id].host_state == "online"
            assert overlay[session_id].terminal_reason == "process_gone"
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.skip(reason="UnmanagedSessionBinding removed; replacement uses kernel SessionConnection")
def test_heartbeat_normalizes_codex_rollout_binding_ids(tmp_path):
    """Older engines may send Codex rollout filename stems. The runtime stores
    only the Codex UUID suffix, so heartbeat ingest must normalize before
    linking the unmanaged binding to the session row."""
    from zerg.models.agents import UnmanagedSessionBinding

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    provider_session_id = "019dc0f3-fb30-71e3-b0fd-2085e7d045a8"
    rollout_id = f"rollout-2026-04-24T16-25-08-{provider_session_id}"

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=uuid4(),
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
                last_activity_at=datetime(2026, 4, 27, 10, 5, tzinfo=timezone.utc),
                provider_session_id=provider_session_id,
                thread_root_session_id=None,
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add(session)
            db.commit()
            session_id = session.id

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
                "unmanaged_session_bindings": [
                    {
                        "machine_id": "cinder",
                        "provider": "codex",
                        "provider_session_id": rollout_id,
                        "source_path": f"/Users/x/.codex/sessions/{rollout_id}.jsonl",
                        "pid": 1234,
                        "process_start_time": "2026-04-27T10:00:00Z",
                        "source_offset": 100,
                        "source_mtime": "2026-04-27T10:05:00Z",
                        "observed_at": "2026-04-27T10:05:00Z",
                    }
                ],
            },
        )
        assert response.status_code == 204, response.text

        with SessionLocal() as db:
            rows = db.query(UnmanagedSessionBinding).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.provider == "codex"
            assert row.provider_session_id == provider_session_id
            assert row.session_id == session_id
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.skip(reason="UnmanagedSessionBinding removed; replacement uses kernel SessionConnection")
def test_heartbeat_migrates_existing_codex_rollout_binding_row(tmp_path):
    """If an older runtime already stored the rollout-prefixed identity, the
    next normalized heartbeat should rewrite that row instead of creating a
    duplicate."""
    from zerg.models.agents import UnmanagedSessionBinding

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    provider_session_id = "019dc0f3-fb30-71e3-b0fd-2085e7d045a8"
    rollout_id = f"rollout-2026-04-24T16-25-08-{provider_session_id}"
    observed_at = datetime(2026, 4, 27, 10, 5, tzinfo=timezone.utc)

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=uuid4(),
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
                last_activity_at=observed_at,
                provider_session_id=provider_session_id,
                thread_root_session_id=None,
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                is_writable_head=1,
            )
            db.add(session)
            db.add(
                UnmanagedSessionBinding(
                    machine_id="cinder",
                    device_id="testclient",
                    provider="codex",
                    provider_session_id=rollout_id,
                    session_id=None,
                    pid=1000,
                    observed_at=observed_at,
                    last_seen_at=observed_at,
                    binding_state="observed",
                )
            )
            db.commit()
            session_id = session.id

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
                "unmanaged_session_bindings": [
                    {
                        "machine_id": "cinder",
                        "provider": "codex",
                        "provider_session_id": rollout_id,
                        "pid": 1234,
                        "process_start_time": "2026-04-27T10:00:00Z",
                        "source_offset": 100,
                        "source_mtime": "2026-04-27T10:05:00Z",
                        "observed_at": "2026-04-27T10:05:00Z",
                    }
                ],
            },
        )
        assert response.status_code == 204, response.text

        with SessionLocal() as db:
            rows = db.query(UnmanagedSessionBinding).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.provider_session_id == provider_session_id
            assert row.session_id == session_id
            assert row.pid == 1234
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.skip(reason="UnmanagedSessionBinding removed; replacement uses kernel SessionConnection")
def test_heartbeat_omitting_unmanaged_bindings_is_fine(tmp_path):
    """Older engines don't send the new field — heartbeat must still accept."""
    from zerg.models.agents import UnmanagedSessionBinding

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.5.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
            },
        )
        assert response.status_code == 204

        with SessionLocal() as db:
            assert db.query(UnmanagedSessionBinding).count() == 0
    finally:
        api_app_ref.dependency_overrides = {}
