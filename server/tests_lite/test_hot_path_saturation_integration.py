from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from datetime import timezone
from threading import Event
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import Response
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", Fernet.generate_key().decode())

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

import zerg.services.managed_control_dispatcher as managed_control_dispatcher_module
import zerg.services.remote_session_launch as remote_session_launch_module
import zerg.services.session_chat_impl as session_chat_impl_module
import zerg.services.session_turns as session_turns_module
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
from zerg.models.live_store import LiveMachineControlOperation
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.routers import agents_sessions as agents_sessions_router
from zerg.routers import health as health_router
from zerg.routers import heartbeat as heartbeat_router
from zerg.routers import runtime as runtime_router
from zerg.routers import session_chat as session_chat_router
from zerg.services.live_archive_outbox import MANAGED_LOCAL_LAUNCH_KIND
from zerg.services.live_archive_outbox import RUNTIME_EVENT_KIND
from zerg.services.live_archive_outbox import SESSION_INPUT_RECEIPT_KIND
from zerg.services.live_archive_outbox import drain_live_archive_outbox
from zerg.services.machine_control_channel import MachineControlChannelRegistry
from zerg.services.machine_control_channel import MachineControlCommandResponse
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.remote_session_launch import RemoteLaunchParams
from zerg.services.remote_session_launch import launch_remote_session
from zerg.services.session_hot_cards import upsert_timeline_card_from_session
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_locks import session_lock_manager
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


class _AutoCompletingMachineWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "turn_id": "hot-path-machine-control-turn-1",
                },
            }
        )


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

    async def send_command_nowait(self, **kwargs):  # type: ignore[override]
        self.sent.append(kwargs)
        return MachineControlCommandResponse(
            transport_ok=True,
            message={
                "type": "command",
                "command_id": kwargs.get("command_id"),
                "command_type": kwargs.get("command_type"),
                "session_id": kwargs.get("session_id", ""),
                "payload": kwargs.get("payload") or {},
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


async def _register_managed_control_channel() -> _AutoCompletingMachineWebSocket:
    websocket = _AutoCompletingMachineWebSocket()
    await get_machine_control_channel_registry().register(
        owner_id=OWNER_ID,
        device_id="cinder",
        machine_name="cinder",
        engine_build="test-engine",
        supports=["codex.send"],
        websocket=websocket,
    )
    return websocket


def _seed_hot_path_rows(session_factory):
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
        seed_managed_kernel_rows(db, session, control_plane="codex_bridge", host_id="cinder")
        upsert_timeline_card_from_session(db, session)
        session_id = session.id
        db.commit()
        return session_id


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
    seeded_session_id = _seed_hot_path_rows(request_factory)

    serializer = WriteSerializer()
    serializer.configure(write_factory)
    live_serializer = WriteSerializer()
    live_serializer.configure(live_write_factory)

    import zerg.data_plane as data_plane_module
    import zerg.database as database_module
    import zerg.services.live_session_inputs as live_session_inputs_module
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
    monkeypatch.setattr(runtime_router, "live_store_configured", lambda: True)
    monkeypatch.setattr(runtime_router, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(runtime_router, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(remote_session_launch_module, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(session_chat_router, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(managed_control_dispatcher_module, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(session_turns_module, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(live_session_inputs_module, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(write_serializer_module, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(write_serializer_module, "get_live_write_serializer", lambda: live_serializer)
    monkeypatch.setattr(session_chat_impl_module, "_schedule_managed_local_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr(session_chat_impl_module, "_schedule_managed_local_active_phase_observation", lambda **_kwargs: None)

    await get_machine_control_channel_registry().clear_for_tests()

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

        managed_control_websocket = await _register_managed_control_channel()
        with request_factory() as input_db:
            source_session = input_db.get(AgentSession, seeded_session_id)
            assert source_session is not None
            input_response = await asyncio.wait_for(
                session_chat_router._create_session_input_response(
                    source_session=source_session,
                    owner_id=OWNER_ID,
                    body=session_chat_router.SessionInputRequest(
                        text="hot input while archive is stalled",
                        intent="auto",
                        client_request_id="hot-input-stall-1",
                    ),
                    db=input_db,
                ),
                timeout=ROUTE_TIMEOUT_SECONDS,
            )
        assert input_response.outcome == "sent"
        assert input_response.input_id is None
        assert input_response.live_input_id is not None
        assert len(managed_control_websocket.sent) == 1
        control_frame = managed_control_websocket.sent[0]
        assert control_frame["command_type"] == "session.send_text"
        assert control_frame["session_id"] == str(seeded_session_id)
        assert str(control_frame["command_id"]).startswith(f"managed-control:{seeded_session_id}:session.send_text:")
        assert control_frame["payload"] == {
            "provider": "codex",
            "text": "hot input while archive is stalled",
        }
        delivery_request_id = None
        with live_factory() as live_db:
            receipt = live_db.get(LiveSessionInputReceipt, input_response.live_input_id)
            assert receipt is not None
            assert receipt.status == INPUT_STATUS_DELIVERED
            assert receipt.client_request_id == "hot-input-stall-1"
            delivery_request_id = receipt.delivery_request_id
            input_outbox = (
                live_db.query(LiveArchiveOutbox)
                .filter(LiveArchiveOutbox.kind == SESSION_INPUT_RECEIPT_KIND)
                .one()
            )
            assert input_outbox.drained_at is None
            control_operation = (
                live_db.query(LiveMachineControlOperation)
                .filter(LiveMachineControlOperation.command_id == control_frame["command_id"])
                .one()
            )
            assert control_operation.status == "succeeded"
            assert control_operation.command_type == "session.send_text"
            assert control_operation.device_id == "cinder"
            assert control_operation.provider == "codex"
            assert json.loads(control_operation.result_json or "{}")["turn_id"] == "hot-path-machine-control-turn-1"
        assert delivery_request_id
        await session_lock_manager.release(str(seeded_session_id), delivery_request_id)

        runtime_payload = runtime_router.RuntimeEventBatchIngest(
            events=[
                {
                    "runtime_key": f"codex:{seeded_session_id}",
                    "session_id": seeded_session_id,
                    "provider": "codex",
                    "device_id": "cinder",
                    "source": "codex_bridge",
                    "kind": "phase_signal",
                    "phase": "running",
                    "tool_name": "Shell",
                    "occurred_at": datetime.now(timezone.utc),
                    "freshness_ms": 60_000,
                    "dedupe_key": "hot-runtime-stall-1",
                    "payload": {},
                }
            ]
        )
        with request_factory() as runtime_db:
            runtime_db.execute(text("SELECT 1"))
            runtime_result = await asyncio.wait_for(
                runtime_router.ingest_runtime_observation_batch(
                    runtime_payload,
                    Response(),
                    runtime_db,
                    SimpleNamespace(device_id="cinder", id="token-1", owner_id=OWNER_ID),
                    None,
                ),
                timeout=ROUTE_TIMEOUT_SECONDS,
            )
        assert runtime_result.accepted == 1
        assert runtime_result.updated_runtime_keys == [f"codex:{seeded_session_id}"]
        with live_factory() as live_db:
            live_runtime = live_db.get(LiveRuntimeState, f"codex:{seeded_session_id}")
            assert live_runtime is not None
            assert live_runtime.phase == "running"
            assert live_runtime.active_tool == "Shell"
            runtime_outbox = (
                live_db.query(LiveArchiveOutbox)
                .filter(LiveArchiveOutbox.kind == RUNTIME_EVENT_KIND)
                .one()
            )
            assert runtime_outbox.drained_at is None

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
        assert launch.launch_state == "launching_unknown"
        assert len(registry.sent) == 1
        with live_factory() as live_db:
            readiness = (
                live_db.query(LiveLaunchReadiness)
                .filter(LiveLaunchReadiness.session_id == str(launch.session_id))
                .one()
            )
            assert readiness.state == "dispatched"
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
        await get_machine_control_channel_registry().clear_for_tests()
        if heartbeat_task is not None and not heartbeat_task.done():
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not blocker.done():
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)
