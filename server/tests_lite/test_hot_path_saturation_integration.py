from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from threading import Event
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", Fernet.generate_key().decode())

import zerg.services.remote_session_launch as remote_session_launch_module
from zerg.database import Base
from zerg.database import get_pool_status
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.database import make_live_write_engine
from zerg.database import make_sessionmaker
from zerg.models import User
from zerg.models.agents import AgentSession
from zerg.models.device_token import DeviceToken
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveLaunchReadiness
from zerg.routers import agents_sessions as agents_sessions_router
from zerg.routers import health as health_router
from zerg.routers import heartbeat as heartbeat_router
from zerg.routers import session_chat as session_chat_router
from zerg.services.live_archive_outbox import MANAGED_LOCAL_LAUNCH_KIND
from zerg.services.live_archive_outbox import drain_live_archive_outbox
from zerg.services.machine_control_channel import MachineControlChannelRegistry
from zerg.services.machine_control_channel import MachineControlCommandResponse
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.remote_session_launch import RemoteLaunchParams
from zerg.services.remote_session_launch import launch_remote_session
from zerg.services.session_hot_cards import upsert_timeline_card_from_session
from zerg.services.write_serializer import WriteSerializer

OWNER_ID = 77
ROUTE_TIMEOUT_SECONDS = 5.0
BLOCKED_WRITER_TIMEOUT_SECONDS = ROUTE_TIMEOUT_SECONDS + 3.0


class _FakeRequest:
    def __init__(self, body: bytes = b"{}") -> None:
        self.client = SimpleNamespace(host="testclient")
        self.headers = {}
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeWebSocket:
    async def send_json(self, _message):  # pragma: no cover - send_command is scripted
        pass


class _StubRegistry(MachineControlChannelRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    async def send_command(self, **kwargs):  # type: ignore[override]
        self.sent.append(kwargs)
        return MachineControlCommandResponse(
            transport_ok=True,
            message={
                "type": "command_result",
                "ok": True,
                "result": {"session_id": kwargs.get("session_id", "")},
            },
        )


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


async def _register_online(registry: MachineControlChannelRegistry) -> None:
    await registry.register(
        owner_id=OWNER_ID,
        device_id="cinder",
        machine_name="cinder",
        engine_build="test",
        supports=["codex.launch"],
        websocket=_FakeWebSocket(),
    )


def _seed_hot_path_rows(session_factory) -> None:
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        db.add(User(id=OWNER_ID, email="owner@example.test", role="ADMIN"))
        db.commit()
        db.add(
            DeviceToken(
                owner_id=OWNER_ID,
                device_id="cinder",
                token_hash="hash-cinder",
            )
        )
        db.commit()
        session = AgentSession(
            provider="codex",
            environment="development",
            project="repo",
            device_id="cinder",
            device_name="cinder",
            cwd="/Users/me/repo",
            git_branch="main",
            started_at=now,
            last_activity_at=now,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            first_user_message_preview="Seeded hot-path question",
            last_visible_text_preview="Seeded hot-path answer",
        )
        db.add(session)
        db.flush()
        upsert_timeline_card_from_session(db, session)
        db.commit()


@pytest.mark.asyncio
async def test_hot_routes_keep_request_pool_free_while_real_writer_is_saturated(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'hot_path_saturation.db'}"
    request_engine = make_engine(db_url, pool_size=1, max_overflow=0)
    write_engine = make_engine(db_url, pool_size=1, max_overflow=0)
    request_factory = make_sessionmaker(request_engine)
    write_factory = make_sessionmaker(write_engine)
    live_url = f"sqlite:///{tmp_path / 'hot_path_live.db'}"
    live_engine = make_live_engine(live_url, pool_size=1, max_overflow=0)
    live_write_engine = make_live_write_engine(live_url)
    live_factory = make_sessionmaker(live_engine)
    live_write_factory = make_sessionmaker(live_write_engine)
    Base.metadata.create_all(bind=request_engine)
    initialize_live_database(live_engine)
    with request_engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(content_text)"))
    _seed_hot_path_rows(request_factory)

    serializer = WriteSerializer()
    serializer.configure(write_factory)
    live_serializer = WriteSerializer()
    live_serializer.configure(live_write_factory)

    import zerg.data_plane as data_plane_module
    import zerg.database as database_module
    import zerg.services.write_serializer as write_serializer_module

    def _cold_store_unavailable(*_args, **_kwargs):
        raise AssertionError("hot health/list/launch paths must not open derived/archive stores")

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_STALE_ACTIVE_MS", "1")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_STALE_QUEUE_DEPTH", "1")
    monkeypatch.setattr(
        "zerg.build_info.load",
        lambda: SimpleNamespace(
            as_dict=lambda: {"commit": "test"},
            version="0.0.0-test",
            channel="test",
            commit_short="test",
            dirty=False,
            qualified_version="0.0.0-test+test",
        ),
    )
    monkeypatch.setattr(database_module, "default_engine", request_engine)
    monkeypatch.setattr(database_module, "default_session_factory", request_factory)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr(data_plane_module, "create_archive_store", _cold_store_unavailable)
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(heartbeat_router, "live_store_configured", lambda: True)
    monkeypatch.setattr(heartbeat_router, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(heartbeat_router, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(remote_session_launch_module, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(session_chat_router, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(session_chat_router, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(session_chat_router, "_MANAGED_LOCAL_STALE_WRITER_MS", 1.0)
    monkeypatch.setattr(write_serializer_module, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(write_serializer_module, "get_live_write_serializer", lambda: live_serializer)

    writer_entered = Event()
    release_writer = Event()
    heartbeat_task = None

    def _block_writer(db):
        db.execute(text("SELECT 1"))
        writer_entered.set()
        assert release_writer.wait(BLOCKED_WRITER_TIMEOUT_SECONDS), "blocked writer was not released"

    blocker = asyncio.create_task(serializer.execute(_block_writer, label="ingest-replay"))
    try:
        assert await asyncio.to_thread(writer_entered.wait, 1)
        assert serializer.writer_active is True

        heartbeat_payload = heartbeat_router.HeartbeatIn(
            version="0.5.0",
            daemon_pid=12345,
            disk_free_bytes=50_000_000_000,
        )
        request_db = request_factory()
        request_db.execute(text("SELECT 1"))
        assert get_pool_status(request_engine)["checked_out"] == 1

        heartbeat_task = asyncio.create_task(
            heartbeat_router.ingest_heartbeat(
                heartbeat_payload,
                _FakeRequest(heartbeat_payload.model_dump_json().encode()),
                request_db,
                SimpleNamespace(device_id="cinder", id="token-1", owner_id=OWNER_ID),
            )
        )
        heartbeat_response = await asyncio.wait_for(heartbeat_task, timeout=ROUTE_TIMEOUT_SECONDS)
        assert heartbeat_response.status_code == 204
        assert get_pool_status(request_engine)["checked_out"] == 0
        assert get_pool_status(write_engine)["checked_out"] == 1
        with live_factory() as live_db:
            assert live_db.query(LiveHeartbeatStamp).filter(LiveHeartbeatStamp.device_id == "cinder").count() == 1

        health = await asyncio.wait_for(
            asyncio.to_thread(
                health_router.health_check,
                _FakeRequest(),
            ),
            timeout=1.0,
        )
        assert health["status"] == "degraded"
        assert health["checks"]["write_serializer"]["writer_active"] is True
        assert health["checks"]["write_serializer"]["status"] == "warn"
        assert health["checks"]["write_serializer"]["archive_degraded"] is True
        assert health["checks"]["write_serializer"]["queue_depth"] == 1
        assert health["checks"]["write_serializer"]["queued_labels"] == ["heartbeat-bookkeeping"]
        assert health["checks"]["live_write_serializer"]["status"] == "pass"
        assert health["checks"]["db_pool"]["checked_out"] == 0

        readyz = await asyncio.wait_for(asyncio.to_thread(health_router.readyz_check), timeout=1.0)
        assert readyz["status"] == "ready_with_archive_degraded"
        assert readyz["reason"] == "archive_write_serializer_stalled"

        with request_factory() as list_db:
            sessions = await asyncio.wait_for(
                agents_sessions_router.list_sessions(
                    project=None,
                    provider=None,
                    environment=None,
                    include_test=False,
                    hide_autonomous=True,
                    device_id=None,
                    days_back=14,
                    query=None,
                    limit=20,
                    offset=0,
                    sort=None,
                    mode="lexical",
                    context_mode="forensic",
                    db=list_db,
                    _auth=SimpleNamespace(),
                    _single=None,
                ),
                timeout=ROUTE_TIMEOUT_SECONDS,
            )
        assert sessions.sessions

        registry = _StubRegistry()
        await _register_online(registry)
        with request_factory() as launch_db:
            launch = await asyncio.wait_for(
                launch_remote_session(
                    launch_db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                ),
                timeout=ROUTE_TIMEOUT_SECONDS,
        )
        assert launch.launch_state == "live"
        assert len(registry.sent) == 1
        with live_factory() as live_db:
            readiness = (
                live_db.query(LiveLaunchReadiness)
                .filter(LiveLaunchReadiness.session_id == str(launch.session_id))
                .one()
            )
            assert readiness.state == "adopted"
            assert readiness.device_id == "cinder"
            assert readiness.provider == "codex"

        await asyncio.sleep(0.01)
        with request_factory() as managed_launch_db:
            managed_result, managed_response = await asyncio.wait_for(
                session_chat_router._launch_managed_local_session_serialized(
                    managed_launch_db,
                    ManagedLocalLaunchParams(
                        owner_id=OWNER_ID,
                        runner_target="cinder",
                        cwd="/Users/me/repo",
                        provider="codex",
                        project="repo",
                        git_branch="main",
                        machine_name="cinder",
                    ),
                ),
                timeout=ROUTE_TIMEOUT_SECONDS,
            )
        assert managed_result is None
        assert managed_response.provider == "codex"
        assert "codex-bridge attach --session-id" in managed_response.attach_command
        with live_factory() as live_db:
            managed_readiness = live_db.get(LiveLaunchReadiness, managed_response.session_id)
            assert managed_readiness is not None
            assert managed_readiness.state == "pending"
            managed_outbox = live_db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.kind == MANAGED_LOCAL_LAUNCH_KIND).one()
            assert managed_outbox.drained_at is None

        release_writer.set()
        await asyncio.wait_for(blocker, timeout=ROUTE_TIMEOUT_SECONDS)
        await _wait_until(lambda: serializer.queue_depth == 0 and not serializer.writer_active)
        with live_factory() as live_db, request_factory() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)
        assert drain_result.drained >= 1
        with request_factory() as archive_db:
            managed_session = archive_db.get(AgentSession, managed_response.session_id)
            assert managed_session is not None
            assert managed_session.provider == "codex"
            assert managed_session.device_id == "cinder"
            assert managed_session.git_branch == "main"
        with live_factory() as live_db:
            managed_readiness = live_db.get(LiveLaunchReadiness, managed_response.session_id)
            assert managed_readiness is not None
            assert managed_readiness.state == "adopted"
    finally:
        release_writer.set()
        if heartbeat_task is not None and not heartbeat_task.done():
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not blocker.done():
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)
