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
from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveRuntimeState
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.managed_control_state import mark_missing_live_control_leases
from zerg.services.managed_control_state import upsert_live_control_leases
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_live_runtime_events
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.session_runtime import session_is_closed_for_input
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
