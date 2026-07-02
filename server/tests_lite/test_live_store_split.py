from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.database as database_module
import zerg.services.session_views as session_views_module
from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveRuntimeState
from zerg.services.agents import AgentsStore
from zerg.services.live_archive_outbox import HEARTBEAT_STAMP_KIND
from zerg.services.live_archive_outbox import RUNTIME_EVENT_KIND
from zerg.services.live_archive_outbox import drain_live_archive_outbox
from zerg.services.live_archive_outbox import enqueue_heartbeat_stamp_outbox
from zerg.services.live_archive_outbox import enqueue_runtime_events_outbox
from zerg.services.live_launch_readiness import get_live_launch_readiness_by_client_request
from zerg.services.live_launch_readiness import get_live_launch_readiness_by_session_id
from zerg.services.live_launch_readiness import latest_live_launch_readiness_map
from zerg.services.live_launch_readiness import reap_expired_live_launch_readiness
from zerg.services.live_launch_readiness import update_live_launch_readiness_state
from zerg.services.live_launch_readiness import upsert_live_launch_readiness
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.managed_control_state import mark_missing_live_control_leases
from zerg.services.managed_control_state import upsert_live_control_leases
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_live_runtime_events
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.session_runtime import session_is_closed_for_input
from zerg.services.session_views import build_session_response
from zerg.services.session_views import latest_live_launch_readiness
from zerg.services.write_serializer import get_live_write_serializer
from zerg.services.write_serializer import get_write_serializer


def test_live_write_serializer_is_distinct_from_archive_serializer():
    assert get_live_write_serializer() is not get_write_serializer()


def test_initialize_live_database_creates_only_live_tables(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")

    initialize_live_database(engine)

    tables = set(inspect(engine).get_table_names())
    assert tables == {
        "live_archive_outbox",
        "live_control_leases",
        "live_heartbeat_stamps",
        "live_launch_readiness",
        "live_runtime_state",
        "live_sessions",
    }
    assert "sessions" not in tables
    assert "agent_heartbeats" not in tables
    assert "events" not in tables


def test_archive_and_live_heartbeat_stamp_columns_stay_in_sync():
    archive_columns = {column.name for column in AgentHeartbeat.__table__.columns if column.name != "id"}
    live_columns = {column.name for column in LiveHeartbeatStamp.__table__.columns if column.name != "id"}

    assert live_columns == archive_columns


def test_archive_and_live_runtime_state_columns_stay_in_sync():
    archive_columns = {column.name for column in SessionRuntimeState.__table__.columns}
    live_columns = {column.name for column in LiveRuntimeState.__table__.columns}

    assert live_columns == archive_columns


def test_live_archive_outbox_drains_heartbeat_to_archive_idempotently(tmp_path):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    heartbeat = {
        "device_id": "live-drain",
        "received_at": now,
        "version": "0.5.0",
        "last_ship_at": None,
        "last_ship_attempt_at": now,
        "last_ship_result": "ok",
        "last_ship_latency_ms": 123,
        "last_ship_http_status": 204,
        "spool_pending": 2,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 3,
        "ship_successes_1h": 3,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "ship_latency_p50_ms_1h": 100,
        "ship_latency_p95_ms_1h": 200,
        "disk_free_bytes": 123_456,
        "is_offline": 0,
        "raw_json": "{\"ok\":true}",
        "sessions_digest": "digest-live-drain",
        "sessions_sequence": 9,
    }

    try:
        with LiveSession() as live_db:
            assert enqueue_heartbeat_stamp_outbox(live_db, heartbeat) is True
            assert enqueue_heartbeat_stamp_outbox(live_db, heartbeat) is False
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10, now=now + timedelta(seconds=1))

        assert result.processed == 1
        assert result.drained == 1
        assert result.failed == 0

        with ArchiveSession() as archive_db:
            rows = archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain").all()
            assert len(rows) == 1
            assert rows[0].version == "0.5.0"
            assert rows[0].spool_pending == 2
            assert rows[0].disk_free_bytes == 123_456
            assert rows[0].raw_json == "{\"ok\":true}"
            assert rows[0].sessions_digest == "digest-live-drain"

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.kind == HEARTBEAT_STAMP_KIND
            assert row.drained_at is not None
            assert row.last_error is None
            assert row.attempts == 1

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain").count() == 1
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_retries_after_live_mark_drained_commit_failure(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    heartbeat = {
        "device_id": "live-drain-retry",
        "received_at": now,
        "version": "0.5.0",
        "spool_pending": 1,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 1,
        "ship_successes_1h": 1,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "disk_free_bytes": 1,
        "is_offline": 0,
        "raw_json": "{}",
    }

    try:
        with LiveSession() as live_db:
            enqueue_heartbeat_stamp_outbox(live_db, heartbeat)
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            real_commit = live_db.commit

            def fail_mark_drained_commit_once():
                raise RuntimeError("live commit failed")

            monkeypatch.setattr(live_db, "commit", fail_mark_drained_commit_once)
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)
            monkeypatch.setattr(live_db, "commit", real_commit)

        assert result.processed == 1
        assert result.drained == 0
        assert result.failed == 1
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain-retry").count() == 1
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is None

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 1
        assert result.drained == 1
        assert result.failed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain-retry").count() == 1
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is not None
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_failure_stays_retryable(tmp_path):
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with LiveSession() as live_db:
            live_db.add(
                LiveArchiveOutbox(
                    idempotency_key="unsupported:1",
                    kind="unsupported.v1",
                    payload_json="{}",
                )
            )
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 1
        assert result.drained == 0
        assert result.failed == 1
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is None
            assert row.attempts == 1
            assert "Unsupported live archive outbox kind" in (row.last_error or "")
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_runtime_state_feeds_existing_runtime_overlay(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: LiveSession)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="live-runtime",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

        event = RuntimeEventIngest(
            runtime_key=runtime_key_for_session("codex", str(session_id)),
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="codex_bridge",
            kind="phase_signal",
            phase="running",
            tool_name="Shell",
            occurred_at=now,
            freshness_ms=60_000,
            dedupe_key="live-runtime-1",
            payload={},
        )
        with LiveSession() as live_db:
            result = ingest_live_runtime_events(live_db, [event])
            live_db.commit()

        assert result.accepted == 1
        assert result.updated_runtime_keys == [event.runtime_key]

        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionRuntimeState).count() == 0
            session = archive_db.query(AgentSession).filter(AgentSession.id == session_id).one()
            runtime_state_map = load_runtime_state_map(archive_db, [session_id])
            runtime_state = runtime_state_map[str(session_id)]
            assert isinstance(runtime_state, LiveRuntimeState)
            overlay = resolve_runtime_overlay(
                session,
                last_activity_at=session.last_activity_at,
                runtime_state_map=runtime_state_map,
                now=now,
            )

        assert overlay.presence_state == "running"
        assert overlay.presence_tool == "Shell"
        assert overlay.runtime_phase == "running"
        assert overlay.runtime_source == "codex_bridge"
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_drains_runtime_event_to_archive(tmp_path):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="runtime-outbox",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

        event = RuntimeEventIngest(
            runtime_key=runtime_key_for_session("codex", str(session_id)),
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="codex_bridge",
            kind="phase_signal",
            phase="running",
            tool_name="Shell",
            occurred_at=now,
            freshness_ms=60_000,
            dedupe_key="runtime-outbox-1",
            payload={},
        )
        with LiveSession() as live_db:
            result = ingest_live_runtime_events(live_db, [event])
            assert enqueue_runtime_events_outbox(live_db, [event]) == 1
            assert enqueue_runtime_events_outbox(live_db, [event]) == 0
            live_db.commit()

        assert result.accepted == 1
        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionRuntimeState).count() == 0
            assert archive_db.query(SessionObservation).count() == 0

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.kind == RUNTIME_EVENT_KIND
            assert row.drained_at is None
            assert "runtime-outbox-1" in row.idempotency_key

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 1
        assert drain_result.drained == 1
        assert drain_result.failed == 0

        with ArchiveSession() as archive_db:
            state = (
                archive_db.query(SessionRuntimeState)
                .filter(SessionRuntimeState.runtime_key == event.runtime_key)
                .one()
            )
            assert state.phase == "running"
            assert state.active_tool == "Shell"
            assert (
                archive_db.query(SessionObservation)
                .filter(SessionObservation.runtime_key == event.runtime_key)
                .count()
                == 1
            )

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is not None
            assert row.last_error is None
            assert row.attempts == 1

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 0
        with ArchiveSession() as archive_db:
            assert (
                archive_db.query(SessionObservation)
                .filter(SessionObservation.runtime_key == event.runtime_key)
                .count()
                == 1
            )
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_runtime_event_retry_is_idempotent(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="runtime-outbox-retry",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

        event = RuntimeEventIngest(
            runtime_key=runtime_key_for_session("codex", str(session_id)),
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="codex_bridge",
            kind="phase_signal",
            phase="running",
            tool_name="Shell",
            occurred_at=now,
            freshness_ms=60_000,
            dedupe_key="runtime-outbox-retry-1",
            payload={},
        )
        with LiveSession() as live_db:
            ingest_live_runtime_events(live_db, [event])
            enqueue_runtime_events_outbox(live_db, [event])
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            real_commit = archive_db.commit

            def fail_archive_commit_once():
                raise RuntimeError("archive commit failed")

            monkeypatch.setattr(archive_db, "commit", fail_archive_commit_once)
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)
            monkeypatch.setattr(archive_db, "commit", real_commit)

        assert drain_result.processed == 1
        assert drain_result.drained == 0
        assert drain_result.failed == 1
        with ArchiveSession() as archive_db:
            assert (
                archive_db.query(SessionRuntimeState)
                .filter(SessionRuntimeState.runtime_key == event.runtime_key)
                .count()
                == 0
            )
            assert (
                archive_db.query(SessionObservation)
                .filter(SessionObservation.runtime_key == event.runtime_key)
                .count()
                == 0
            )
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is None
            assert row.attempts == 1
            assert "archive commit failed" in (row.last_error or "")

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 1
        assert drain_result.drained == 1
        assert drain_result.failed == 0

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            row.drained_at = None
            live_db.commit()
        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 1
        assert drain_result.drained == 1
        assert drain_result.failed == 0
        with ArchiveSession() as archive_db:
            assert (
                archive_db.query(SessionRuntimeState)
                .filter(SessionRuntimeState.runtime_key == event.runtime_key)
                .count()
                == 1
            )
            assert (
                archive_db.query(SessionObservation)
                .filter(SessionObservation.runtime_key == event.runtime_key)
                .count()
                == 1
            )
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_launch_readiness_projects_and_reaps(tmp_path):
    now = datetime.now(timezone.utc)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    expired_session_id = uuid4()

    try:
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="live_control",
                state="pending",
                command_id=f"launch-{session_id}",
                client_request_id="launch-live-1",
                machine_id="cinder",
                project="repo",
                expires_at=now + timedelta(minutes=2),
                now=now,
            )
            upsert_live_launch_readiness(
                live_db,
                session_id=expired_session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="live_control",
                state="pending",
                command_id=f"launch-{expired_session_id}",
                client_request_id="launch-live-expired",
                machine_id="cinder",
                project="repo",
                expires_at=now - timedelta(seconds=1),
                now=now - timedelta(minutes=5),
            )
            live_db.commit()

        with LiveSession() as live_db:
            readiness = get_live_launch_readiness_by_client_request(
                live_db,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                client_request_id="launch-live-1",
            )
            assert readiness is not None
            assert readiness.session_id == session_id
            assert readiness.launch_state == "launching"

            assert update_live_launch_readiness_state(
                live_db,
                session_id=session_id,
                state="adopted",
                clear_expires=True,
                now=now + timedelta(seconds=1),
            )
            removed = reap_expired_live_launch_readiness(live_db, now=now, limit=10)
            live_db.commit()

        assert removed == 1
        with LiveSession() as live_db:
            row = live_db.get(LiveLaunchReadiness, str(session_id))
            assert row is not None
            assert row.state == "adopted"
            assert row.expires_at is None
            assert live_db.get(LiveLaunchReadiness, str(expired_session_id)) is None
    finally:
        live_engine.dispose()


def test_live_launch_readiness_session_map_ignores_expired_rows(tmp_path):
    now = datetime.now(timezone.utc)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    expired_session_id = uuid4()

    try:
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="one_shot",
                state="pending",
                command_id=f"launch-{session_id}",
                client_request_id="launch-session-map",
                machine_id="cinder",
                project="repo",
                expires_at=now + timedelta(minutes=2),
                now=now,
            )
            upsert_live_launch_readiness(
                live_db,
                session_id=expired_session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="one_shot",
                state="pending",
                command_id=f"launch-{expired_session_id}",
                client_request_id="launch-session-map-expired",
                machine_id="cinder",
                project="repo",
                expires_at=now - timedelta(seconds=1),
                now=now - timedelta(minutes=5),
            )
            live_db.commit()

        with LiveSession() as live_db:
            readiness = get_live_launch_readiness_by_session_id(live_db, session_id=session_id, now=now)
            assert readiness is not None
            assert readiness.session_id == session_id
            assert readiness.execution_lifetime == "one_shot"
            assert readiness.launch_state == "launching"

            assert get_live_launch_readiness_by_session_id(live_db, session_id=expired_session_id, now=now) is None
            readiness_map = latest_live_launch_readiness_map(live_db, [session_id, expired_session_id], now=now)

        assert set(readiness_map) == {session_id}
    finally:
        live_engine.dispose()


def test_fresh_live_launch_readiness_feeds_session_response_before_archive(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: LiveSession)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="launch-readiness",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.flush()
            cold_attempt = SessionLaunchAttempt(
                session_id=session.id,
                provider="codex",
                host_id="cinder",
                owner_id=77,
                execution_lifetime="one_shot",
                client_request_id="launch-hot-wins",
                command_id=f"launch-{session.id}",
                state="failed",
                error_code="provider_launch_failed",
                error_message="archive saw a stale failure",
                expires_at=None,
            )
            archive_db.add(cold_attempt)
            archive_db.commit()
            session_id = session.id

        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="one_shot",
                state="pending",
                command_id=f"launch-{session_id}",
                client_request_id="launch-hot-wins",
                machine_id="cinder",
                project="repo",
                expires_at=now + timedelta(minutes=2),
                now=now,
            )
            live_db.commit()

        with ArchiveSession() as archive_db:
            session = archive_db.get(AgentSession, session_id)
            cold_attempt = (
                archive_db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.session_id == session_id).one()
            )
            live_map = latest_live_launch_readiness([session_id], now=now)
            monkeypatch.setattr(
                session_views_module,
                "_latest_launch_attempt",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("hot launch readiness hit archive")),
            )
            response = build_session_response(
                AgentsStore(archive_db),
                session,
                last_activity_at=session.started_at,
                launch_readiness=live_map[session_id],
            )

            assert response.launch_state == "launching"
            assert response.execution_lifetime == "one_shot"
            assert response.launch_error_code is None
            assert response.launch_error_message is None

            expired_live_map = latest_live_launch_readiness([session_id], now=now + timedelta(minutes=5))
            fallback = build_session_response(
                AgentsStore(archive_db),
                session,
                last_activity_at=session.started_at,
                launch_attempt=cold_attempt,
                launch_readiness=expired_live_map.get(session_id),
            )

            assert fallback.launch_state == "launch_failed"
            assert fallback.launch_error_code == "provider_launch_failed"
            assert "archive saw a stale failure" in (fallback.launch_error_message or "")
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_control_lease_feeds_managed_control_overlay(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: LiveSession)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="live-control",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

        lease = SimpleNamespace(
            session_id=session_id,
            provider="codex",
            machine_id="cinder",
            state="attached",
            sequence=42,
            bridge_status="ready",
            thread_subscription_status="active",
            observed_at=now,
            lease_ttl_ms=60_000,
        )
        with LiveSession() as live_db:
            touched = upsert_live_control_leases(live_db, [lease], device_id="cinder", received_at=now)
            live_db.commit()

        assert touched == {session_id}

        with ArchiveSession() as archive_db:
            overlay = load_managed_control_state_map(archive_db, [session_id])[session_id]

        assert overlay.control_state == "online"
        assert overlay.lease_state == "attached"
        assert overlay.device_id == "cinder"
        assert overlay.machine_id == "cinder"
        assert overlay.sequence == 42
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_fresh_live_control_missing_beats_stale_archive_online(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=5)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: LiveSession)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="live-control-missing",
                device_id="cinder",
                started_at=old,
                last_activity_at=old,
            )
            archive_db.add(session)
            archive_db.flush()
            _thread, _run, conn = seed_managed_kernel_rows(
                archive_db,
                session,
                control_plane="codex_bridge",
                state="attached",
            )
            conn.device_id = "cinder"
            conn.last_health_at = old
            archive_db.commit()
            session_id = session.id

        lease = SimpleNamespace(
            session_id=session_id,
            provider="codex",
            machine_id="cinder",
            state="attached",
            sequence=1,
            bridge_status="ready",
            thread_subscription_status="active",
            observed_at=old,
            lease_ttl_ms=60_000,
        )
        with LiveSession() as live_db:
            upsert_live_control_leases(live_db, [lease], device_id="cinder", received_at=old)
            mark_missing_live_control_leases(live_db, [], device_id="cinder", received_at=now)
            live_db.commit()

        with ArchiveSession() as archive_db:
            overlay = load_managed_control_state_map(archive_db, [session_id])[session_id]

        assert overlay.control_state == "offline"
        assert overlay.lease_state == "missing"
        assert overlay.reason == "missing_from_snapshot"
        assert overlay.last_control_seen_at == now
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_terminal_runtime_state_closes_session_for_input(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: LiveSession)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="live-terminal",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

        event = RuntimeEventIngest(
            runtime_key=runtime_key_for_session("codex", str(session_id)),
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="codex_bridge",
            kind="terminal_signal",
            occurred_at=now,
            freshness_ms=0,
            dedupe_key="live-terminal-1",
            payload={
                "terminal_state": "process_gone",
                "terminal_reason": "process_gone",
                "terminal_source": "codex_bridge",
            },
        )
        with LiveSession() as live_db:
            ingest_live_runtime_events(live_db, [event])
            live_db.commit()

        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionRuntimeState).count() == 0
            assert session_is_closed_for_input(archive_db, session_id) is True
    finally:
        archive_engine.dispose()
        live_engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_live_stamp_returns_while_archive_bookkeeping_waits(tmp_path, monkeypatch):
    import zerg.routers.heartbeat as heartbeat_router

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(heartbeat_router, "live_store_configured", lambda: True)

    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    old_stamp_at = datetime.now(timezone.utc) - timedelta(days=31)
    with LiveSession() as live_db:
        live_db.add(
            LiveHeartbeatStamp(
                device_id="live-split",
                received_at=old_stamp_at,
                version="old",
            )
        )
        live_db.commit()

    live_stamp_done = asyncio.Event()
    archive_bookkeeping_started = asyncio.Event()
    release_archive_bookkeeping = asyncio.Event()
    observations: dict[str, int] = {}

    class LiveSerializer:
        is_configured = True

        async def execute(self, fn, **kwargs):
            assert kwargs["label"] == "heartbeat-stamp"
            observations["archive_pool_checked_out_at_live_write"] = archive_engine.pool.checkedout()
            with LiveSession() as live_db:
                result = fn(live_db)
                live_db.commit()
            live_stamp_done.set()
            return result

    class ArchiveSerializer:
        is_configured = True

        async def execute(self, fn, **kwargs):
            assert kwargs["label"] == "heartbeat-bookkeeping"
            archive_bookkeeping_started.set()
            await release_archive_bookkeeping.wait()
            return {}

        async def execute_after_closing_request_session(self, *_args, **_kwargs):  # pragma: no cover - guard
            raise AssertionError("live-configured heartbeat stamp must not use archive serializer")

    class _FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self, body: bytes) -> None:
            self._body = body

        async def body(self) -> bytes:
            return self._body

    monkeypatch.setattr(heartbeat_router, "get_live_write_serializer", lambda: LiveSerializer())
    monkeypatch.setattr(heartbeat_router, "get_write_serializer", lambda: ArchiveSerializer())

    payload = heartbeat_router.HeartbeatIn(
        version="0.5.0",
        daemon_pid=12345,
        spool_pending_count=2,
        parse_error_count_1h=0,
        consecutive_ship_failures=0,
        disk_free_bytes=50_000_000_000,
        is_offline=False,
        sessions_digest="digest-1",
        sessions_sequence=7,
        managed_sessions=[
            heartbeat_router.ManagedSessionLeaseIn(
                session_id=session_id,
                provider="codex",
                machine_id="live-split",
                state="attached",
                phase="idle",
                bridge_status="ready",
                thread_subscription_status="active",
                lease_ttl_ms=60_000,
                sequence=7,
            )
        ],
    )

    request_db = ArchiveSession()
    request_db.execute(text("SELECT 1"))
    try:
        response = await asyncio.wait_for(
            heartbeat_router.ingest_heartbeat(
                payload,
                _FakeRequest(payload.model_dump_json().encode()),
                request_db,
                SimpleNamespace(device_id="live-split", id="token-1"),
            ),
            timeout=0.5,
        )
        assert response.status_code == 204
        assert live_stamp_done.is_set()
        await asyncio.wait_for(archive_bookkeeping_started.wait(), timeout=0.5)
        assert not release_archive_bookkeeping.is_set()

        with LiveSession() as live_db:
            row = live_db.query(LiveHeartbeatStamp).filter(LiveHeartbeatStamp.device_id == "live-split").one()
            assert row.spool_pending == 2
            assert row.sessions_digest == "digest-1"
            assert row.sessions_sequence == 7
            assert row.version == "0.5.0"
            control = live_db.query(LiveControlLease).filter(LiveControlLease.session_id == str(session_id)).one()
            assert control.device_id == "live-split"
            assert control.provider == "codex"
            assert control.state == "attached"
            assert control.sequence == 7
            outbox = live_db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.kind == HEARTBEAT_STAMP_KIND).one()
            assert outbox.drained_at is None
            assert "live-split" in outbox.idempotency_key

        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-split").count() == 0
    finally:
        release_archive_bookkeeping.set()
        await asyncio.sleep(0)
        archive_engine.dispose()
        live_engine.dispose()

    assert observations == {"archive_pool_checked_out_at_live_write": 0}


@pytest.mark.asyncio
async def test_heartbeat_live_store_requires_configured_live_serializer(tmp_path, monkeypatch):
    import zerg.routers.heartbeat as heartbeat_router

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(heartbeat_router, "live_store_configured", lambda: True)

    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    class UnconfiguredLiveSerializer:
        is_configured = False

    class ArchiveSerializer:
        is_configured = True

    class _FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self, body: bytes) -> None:
            self._body = body

        async def body(self) -> bytes:
            return self._body

    monkeypatch.setattr(heartbeat_router, "get_live_write_serializer", lambda: UnconfiguredLiveSerializer())
    monkeypatch.setattr(heartbeat_router, "get_write_serializer", lambda: ArchiveSerializer())

    payload = heartbeat_router.HeartbeatIn(
        version="0.5.0",
        daemon_pid=12345,
        spool_pending_count=0,
        parse_error_count_1h=0,
        consecutive_ship_failures=0,
        disk_free_bytes=1,
        is_offline=False,
    )

    request_db = ArchiveSession()
    try:
        with pytest.raises(heartbeat_router.HTTPException) as exc:
            await heartbeat_router.ingest_heartbeat(
                payload,
                _FakeRequest(payload.model_dump_json().encode()),
                request_db,
                SimpleNamespace(device_id="live-unconfigured", id="token-1"),
            )
    finally:
        archive_engine.dispose()

    assert exc.value.status_code == 503
    assert "Live Store write serializer is not configured" in str(exc.value.detail)
