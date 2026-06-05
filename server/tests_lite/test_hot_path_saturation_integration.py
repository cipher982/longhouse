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

from zerg.database import Base
from zerg.database import get_pool_status
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import User
from zerg.models.agents import AgentSession
from zerg.models.device_token import DeviceToken
from zerg.routers import agents_sessions as agents_sessions_router
from zerg.routers import health as health_router
from zerg.routers import heartbeat as heartbeat_router
from zerg.services.machine_control_channel import MachineControlChannelRegistry
from zerg.services.machine_control_channel import MachineControlCommandResponse
from zerg.services.remote_session_launch import RemoteLaunchParams
from zerg.services.remote_session_launch import launch_remote_session
from zerg.services.write_serializer import WriteSerializer

OWNER_ID = 77


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
        db.add(
            AgentSession(
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
        )
        db.commit()


@pytest.mark.asyncio
async def test_hot_routes_keep_request_pool_free_while_real_writer_is_saturated(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'hot_path_saturation.db'}"
    request_engine = make_engine(db_url, pool_size=1, max_overflow=0)
    write_engine = make_engine(db_url, pool_size=1, max_overflow=0)
    request_factory = make_sessionmaker(request_engine)
    write_factory = make_sessionmaker(write_engine)
    Base.metadata.create_all(bind=request_engine)
    with request_engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(content_text)"))
    _seed_hot_path_rows(request_factory)

    serializer = WriteSerializer()
    serializer.configure(write_factory)

    import zerg.database as database_module
    import zerg.services.write_serializer as write_serializer_module

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(database_module, "default_engine", request_engine)
    monkeypatch.setattr(database_module, "default_session_factory", request_factory)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr(heartbeat_router, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(write_serializer_module, "get_write_serializer", lambda: serializer)

    writer_entered = Event()
    release_writer = Event()
    heartbeat_task = None

    def _block_writer(db):
        db.execute(text("SELECT 1"))
        writer_entered.set()
        assert release_writer.wait(2), "blocked writer was not released"

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
        await _wait_until(lambda: serializer.queue_depth == 1)
        assert get_pool_status(request_engine)["checked_out"] == 0
        assert get_pool_status(write_engine)["checked_out"] == 1

        health = await asyncio.wait_for(
            asyncio.to_thread(
                health_router.health_check,
                _FakeRequest(),
            ),
            timeout=1.0,
        )
        assert health["checks"]["write_serializer"]["writer_active"] is True
        assert health["checks"]["write_serializer"]["queue_depth"] == 1
        assert health["checks"]["db_pool"]["checked_out"] == 0

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
                timeout=1.0,
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
                timeout=1.0,
            )
        assert launch.launch_state == "live"
        assert len(registry.sent) == 1

        release_writer.set()
        heartbeat_response = await asyncio.wait_for(heartbeat_task, timeout=1.0)
        await asyncio.wait_for(blocker, timeout=1.0)
        assert heartbeat_response.status_code == 204
    finally:
        release_writer.set()
        if heartbeat_task is not None and not heartbeat_task.done():
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not blocker.done():
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)
