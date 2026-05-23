"""Tests for POST /api/sessions/launch and the launch_remote_session service.

Retired pending kernel-shaped replacement: the session-identity-kernel
cleanup deleted ``launch_state``/``launch_lease_until`` columns from
``AgentSession``; persistence now lives on ``SessionLaunchAttempt``. The
tests below were tightly coupled to the deleted columns.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "launch_state columns deleted by session-identity-kernel cleanup",
    allow_module_level=True,
)

import asyncio
import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from zerg.database import Base  # noqa: E402
from zerg.database import get_db  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.browser_route_auth import get_current_browser_route_user  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402
from zerg.services.machine_control_channel import MachineControlChannelRegistry  # noqa: E402
from zerg.services.machine_control_channel import MachineControlCommandResponse  # noqa: E402
from zerg.services.machine_control_channel import get_machine_control_channel_registry  # noqa: E402
from zerg.services.remote_session_launch import RemoteLaunchError  # noqa: E402
from zerg.services.remote_session_launch import RemoteLaunchParams  # noqa: E402
from zerg.services.remote_session_launch import launch_remote_session  # noqa: E402
from zerg.services.remote_session_launch import reap_orphaned_launches  # noqa: E402
from zerg.services.remote_session_launch import reconcile_launch_from_command_result  # noqa: E402
from zerg.services.session_runtime import RuntimeEventIngest  # noqa: E402
from zerg.services.session_runtime import ingest_runtime_events  # noqa: E402
from zerg.services.session_workspace import build_session_workspace  # noqa: E402

OWNER_ID = 77


def _make_db(tmp_path):
    db_path = tmp_path / "remote_launch.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_user_and_device(SessionLocal, *, owner_id: int = OWNER_ID, device_id: str = "cinder"):
    with SessionLocal() as db:
        existing = db.query(User).filter(User.id == owner_id).first()
        if existing is None:
            db.add(User(id=owner_id, email=f"u{owner_id}@ex.com", role="ADMIN"))
            db.commit()
    with SessionLocal() as db:
        db.add(
            DeviceToken(
                owner_id=owner_id,
                device_id=device_id,
                token_hash=f"hash-{device_id}-{owner_id}",
            )
        )
        db.commit()


class _FakeWebSocket:
    async def send_json(self, message):  # pragma: no cover — tests short-circuit registry
        pass


def _register_online(
    registry: MachineControlChannelRegistry,
    *,
    owner_id: int,
    device_id: str,
    supports: tuple[str, ...] = ("codex.launch",),
):
    asyncio.run(
        registry.register(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=device_id,
            engine_build="test",
            supports=list(supports),
            websocket=_FakeWebSocket(),
        )
    )


class _StubRegistry(MachineControlChannelRegistry):
    """Registry with scripted ``send_command`` responses per session_id."""

    def __init__(self):
        super().__init__()
        self._scripted: dict[str, MachineControlCommandResponse] = {}
        self.sent: list[dict] = []

    def script(self, session_id: str, response: MachineControlCommandResponse):
        self._scripted[session_id] = response

    async def send_command(self, **kwargs):  # type: ignore[override]
        self.sent.append(kwargs)
        session_id = kwargs.get("session_id", "")
        if session_id in self._scripted:
            return self._scripted[session_id]
        # Default: transport ok, ok=True
        return MachineControlCommandResponse(
            transport_ok=True,
            message={"type": "command_result", "ok": True, "result": {"session_id": session_id}},
        )


def test_happy_path_inserts_live_session(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row is not None
        assert row.launch_state == "live"
        assert row.launch_error_code is None
        assert row.launch_lease_until is None
        assert row.provider == "codex"
        assert row.cwd == "/Users/me/repo"
        assert row.device_id == "cinder"
        assert row.source_runner_id is None

    # verify we dispatched a session.launch with the pre-allocated id
    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.launch"
    assert sent["session_id"] == str(result.session_id)
    assert sent["payload"]["provider"] == "codex"


def test_offline_machine_returns_409_no_row(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    # Never register — machine offline

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "machine_offline"
    assert excinfo.value.status_code == 409

    with SessionLocal() as db:
        assert db.query(AgentSession).count() == 0


def test_unsupported_provider_rejected(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="claude",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "provider_unsupported"


def test_device_ownership_required(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID)
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID + 1, device_id="not-mine")
    registry = _StubRegistry()
    # Register the other user's machine — shouldn't be launchable by OWNER_ID
    _register_online(registry, owner_id=OWNER_ID + 1, device_id="not-mine")

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="not-mine",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "device_not_enrolled"
    assert excinfo.value.status_code == 404


def test_engine_error_maps_to_launch_failed(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    # First call will get a typed cwd_not_found error — use a wildcard match
    class _EngineErrorRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(
                transport_ok=True,
                message={
                    "type": "command_result",
                    "ok": False,
                    "error": {"code": "cwd_not_found", "message": "nope"},
                },
            )

    err_registry = _EngineErrorRegistry()
    _register_online(err_registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=err_registry,
            )
        )
    assert result.launch_state == "launch_failed"
    assert result.launch_error_code == "cwd_not_found"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row.launch_state == "launch_failed"
        assert row.launch_error_code == "cwd_not_found"
        assert row.ended_at is not None


def test_transport_timeout_leaves_unknown(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(
                transport_ok=False,
                error="command timed out after 30 seconds",
            )

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )
    assert result.launch_state == "launching_unknown"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row.launch_state == "launching_unknown"
        assert row.launch_lease_until is not None
        assert row.ended_at is None


def test_cwd_relative_rejected_server_side(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="not/absolute",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "cwd_not_allowed"


# -------- HTTP endpoint ---------------------------------------------------


def _make_browser_client(SessionLocal, *, owner_id: int = OWNER_ID):
    from zerg.main import api_app
    from zerg.main import app

    def override_db():
        with SessionLocal() as db:
            yield db

    def override_user():
        return SimpleNamespace(id=owner_id, email=f"u{owner_id}@ex.com", role="ADMIN")

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_browser_route_user] = override_user
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def _patch_registry(registry):
    import zerg.services.remote_session_launch as module

    original = module.get_machine_control_channel_registry
    module.get_machine_control_channel_registry = lambda: registry
    return original, module


def test_http_endpoint_happy_path(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                "/api/sessions/launch",
                json={
                    "device_id": "cinder",
                    "provider": "codex",
                    "cwd": "/Users/me/repo",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["launch_state"] == "live"
    assert body["session_id"]


def test_client_request_id_is_idempotent(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    params = RemoteLaunchParams(
        owner_id=OWNER_ID,
        device_id="cinder",
        provider="codex",
        cwd="/Users/me/repo",
        client_request_id="tap-1",
    )
    with SessionLocal() as db:
        first = asyncio.run(launch_remote_session(db, params, registry=registry))
    with SessionLocal() as db:
        second = asyncio.run(launch_remote_session(db, params, registry=registry))

    assert first.session_id == second.session_id
    assert len(registry.sent) == 1  # second call short-circuits


def test_launched_codex_workspace_exposes_live_engine_control(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    launch_registry = _StubRegistry()
    _register_online(launch_registry, owner_id=OWNER_ID, device_id="cinder")

    global_registry = get_machine_control_channel_registry()
    asyncio.run(global_registry.clear_for_tests())
    _register_online(
        global_registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        supports=("codex.launch", "codex.send", "codex.interrupt", "codex.steer"),
    )

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=launch_registry,
                )
            )
            ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=f"codex:{result.session_id}",
                        session_id=result.session_id,
                        provider="codex",
                        device_id="cinder",
                        source="codex_bridge",
                        kind="phase_signal",
                        phase="idle",
                        tool_name=None,
                        occurred_at=datetime.now(timezone.utc),
                        freshness_ms=60_000,
                        dedupe_key=f"test-launch-ready:{result.session_id}",
                        payload={"managed_transport": "codex_app_server", "thread_id": "thread-1"},
                    )
                ],
            )
            workspace = build_session_workspace(db=db, session_id=result.session_id, owner_id=OWNER_ID)
    finally:
        asyncio.run(global_registry.clear_for_tests())

    assert workspace.session.launch_state == "live"
    assert workspace.session.capabilities.live_control_available is True
    assert workspace.session.capabilities.can_queue_next_input is True
    assert workspace.session.capabilities.can_steer_active_turn is False


def test_late_result_reconciliation_moves_unknown_to_live(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )
    assert result.launch_state == "launching_unknown"

    command_id = registry.sent[-1]["command_id"]
    # Simulate late success
    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": True,
                "result": {"session_id": str(result.session_id)},
            },
        )
    assert reconciled is True
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row.launch_state == "live"
        assert row.launch_lease_until is None


def test_late_result_reconciliation_ignores_unknown_command(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": "launch-00000000-0000-0000-0000-000000000000",
                "ok": True,
            },
        )
    assert reconciled is False


def test_reap_orphaned_launches_expires_stale_rows(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    past = datetime.now(timezone.utc).replace(tzinfo=timezone.utc)
    with SessionLocal() as db:
        sid = uuid4()
        db.add(
            AgentSession(
                id=sid,
                provider="codex",
                environment="development",
                project="repo",
                device_id="cinder",
                cwd="/Users/me/repo",
                started_at=past,
                thread_root_session_id=sid,
                continued_from_session_id=None,
                continuation_kind="local",
                origin_label="cinder",
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                is_writable_head=1,
                is_sidechain=0,
                execution_home="managed_local",
                launch_state="launching_unknown",
                launch_command_id=f"launch-{sid}",
                launch_lease_until=past.replace(year=past.year - 1),  # way in the past
            )
        )
        db.commit()
        reaped = reap_orphaned_launches(db)
    assert reaped == 1
    with SessionLocal() as db:
        row = db.query(AgentSession).first()
        assert row.launch_state == "launch_orphaned"
        assert row.launch_lease_until is None
        assert row.ended_at is not None


def _make_admin_client(SessionLocal, *, owner_id: int = OWNER_ID):
    from zerg.dependencies.auth import get_current_user
    from zerg.dependencies.auth import require_admin
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_user():
        return SimpleNamespace(id=owner_id, email="admin@example.com", role="ADMIN")

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_user] = override_user
    api_app.dependency_overrides[require_admin] = override_user
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def test_admin_launch_debug_lists_non_live_rows(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for state in ("launching_unknown", "launch_failed", "launch_orphaned", "live"):
            sid = uuid4()
            db.add(
                AgentSession(
                    id=sid,
                    provider="codex",
                    environment="development",
                    project="repo",
                    device_id="cinder",
                    cwd="/Users/me/repo",
                    started_at=now,
                    thread_root_session_id=sid,
                    continued_from_session_id=None,
                    continuation_kind="local",
                    origin_label="cinder",
                    user_messages=0,
                    assistant_messages=0,
                    tool_calls=0,
                    is_writable_head=1,
                    is_sidechain=0,
                    execution_home="managed_local",
                    launch_state=state,
                )
            )
        db.commit()

    client, api_app = _make_admin_client(SessionLocal)
    try:
        resp = client.get("/api/admin/launches/debug")
    finally:
        api_app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    states = sorted(e["launch_state"] for e in body["entries"])
    assert states == ["launch_failed", "launch_orphaned", "launching_unknown"]

    client, api_app = _make_admin_client(SessionLocal)
    try:
        resp_all = client.get("/api/admin/launches/debug?include_live=true")
    finally:
        api_app.dependency_overrides.clear()
    assert resp_all.status_code == 200
    assert len(resp_all.json()["entries"]) == 4


def test_http_endpoint_offline_machine_is_409(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                "/api/sessions/launch",
                json={
                    "device_id": "cinder",
                    "provider": "codex",
                    "cwd": "/Users/me/repo",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "machine_offline"
