from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.managed_local_launcher import _derive_project
from zerg.services.managed_local_launcher import _initial_provider_session_id_for_spawn
from zerg.services.session_kernel_projection import project_provider_session_id
from zerg.services.session_kernel_projection import project_session_control_fields
from zerg.services.session_pubsub import TOPIC_TIMELINE
from zerg.services.session_pubsub import get_pubsub
from zerg.services.session_pubsub import reset_pubsub_for_test


def _make_db(tmp_path):
    db_path = tmp_path / "test_managed_local_launch.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_device_client(db_session, device_token):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_device_token():
        return device_token

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_device_token
    return TestClient(app, backend="asyncio"), api_app


def _seed_user_and_runner(db):
    user = User(email="managed-local@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    runner = Runner(
        owner_id=user.id,
        name="cinder",
        availability_policy="always_on",
        capabilities=["exec.full"],
        status="online",
        auth_secret_hash="secret-hash",
        runner_metadata={"install_mode": "desktop"},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return user, runner


def _project_control(db, session):
    capabilities = project_session_capabilities(db, session_id=session.id)
    return capabilities, project_session_control_fields(db, session, capabilities=capabilities)


def test_managed_local_derived_project_ignores_generic_workspace():
    assert _derive_project("/private/tmp/longhouse/workspace", None) == "managed-local"
    assert _derive_project("/private/tmp/longhouse/workspace", "explicit") == "explicit"


def test_initial_provider_session_id_for_spawn_is_provider_specific():
    claude_provider_id = _initial_provider_session_id_for_spawn("claude")
    assert claude_provider_id
    assert _initial_provider_session_id_for_spawn("codex") is None
    assert _initial_provider_session_id_for_spawn("opencode") is None
    assert _initial_provider_session_id_for_spawn("antigravity") is None


def test_managed_local_launch_response_contract_rejects_missing_claude_provider_id():
    from zerg.services.session_chat_impl import ManagedLocalSessionLaunchResponse
    from zerg.services.session_chat_impl import _validate_managed_local_launch_response_contract
    from zerg.session_execution_home import ManagedSessionTransport
    from zerg.session_execution_home import SessionExecutionHome
    from zerg.session_loop_mode import SessionLoopMode

    response = ManagedLocalSessionLaunchResponse(
        session_id="session-123",
        provider="claude",
        provider_session_id=None,
        execution_home=SessionExecutionHome.MANAGED_LOCAL,
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE,
        loop_mode=SessionLoopMode.ASSIST,
        source_runner_id=1,
        source_runner_name="cinder",
        managed_session_name="demo",
        attach_command="",
    )

    with pytest.raises(RuntimeError, match="missing provider_session_id"):
        _validate_managed_local_launch_response_contract(
            session_id="session-123",
            response=response,
        )


def test_this_device_launch_discards_session_when_response_contract_fails(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    reset_pubsub_for_test()
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, _runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        timeline_seq = get_pubsub().peek_latest_seq(TOPIC_TIMELINE)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )
        monkeypatch.setattr(
            managed_local_launcher,
            "_initial_provider_session_id_for_spawn",
            lambda _provider: None,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "claude",
                    "project": "demo",
                    "native_claude_channels_available": True,
                },
            )
        finally:
            api_app.dependency_overrides = {}

        assert response.status_code == 500, response.text
        assert response.json()["detail"] == "Managed local launch failed"
        assert db.query(AgentSession).count() == 0
        assert db.query(SessionRuntimeState).count() == 0
        assert get_pubsub().peek_latest_seq(TOPIC_TIMELINE) == timeline_seq


def test_browser_managed_local_launch_route_is_absent():
    from zerg.main import app

    with TestClient(app, backend="asyncio") as client:
        response = client.post(
            "/api/sessions/managed-local",
            json={
                "runner_target": "runner:1",
                "cwd": "/tmp/demo",
                "provider": "claude",
            },
        )

    assert response.status_code == 404


def test_this_device_launch_allows_offline_runner_for_local_provider_start(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: False),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "claude",
                    "project": "demo",
                    "native_claude_channels_available": True,
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()

    assert response.status_code == 200, response.text
    assert payload["source_runner_id"] == runner.id
    assert payload["source_runner_name"] == "cinder"
    assert session.device_id == "cinder"


def test_this_device_launch_uses_machine_name_as_dev_device_id(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner = _seed_user_and_runner(db)
        client, api_app = _make_device_client(db, None)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "antigravity",
                    "project": "demo",
                    "machine_name": "cinder",
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()

    assert response.status_code == 200, response.text
    assert payload["source_runner_id"] is None
    assert payload["source_runner_name"] == "cinder"
    assert session.device_id == "cinder"
    assert session.provider == "antigravity"


def test_this_device_launch_does_not_require_runner_record(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = User(email="managed-local@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: False),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "codex",
                    "project": "demo",
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()

    assert response.status_code == 200, response.text
    assert payload["source_runner_id"] is None
    assert payload["source_runner_name"] == "cinder"
    assert session.device_id == "cinder"
    _capabilities, control = _project_control(db, session)
    assert control.source_runner_id is None


def test_this_device_launch_uses_managed_launch_write_serializer(monkeypatch, tmp_path):
    from zerg.routers import session_chat
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)
    calls: list[dict] = []

    class RecordingSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0

        async def repair_idle_queue(self):
            return False

        async def execute_or_direct(self, fn, fallback_db, **kwargs):
            calls.append(kwargs)
            return fn(fallback_db)

    with SessionLocal() as db:
        user = User(email="managed-local@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(session_chat, "get_write_serializer", lambda: RecordingSerializer())
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: False),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "codex",
                    "project": "demo",
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()

    assert response.status_code == 200, response.text
    assert calls == [{"label": "managed-launch", "auto_commit": False}]
    assert payload["managed_transport"] == "codex_app_server"
    capabilities, control = _project_control(db, session)
    assert capabilities.managed_transport.value == "codex_app_server"
    assert control.source_runner_id is None


def test_this_device_launch_reports_503_when_serializer_writer_is_stale(monkeypatch):
    from zerg.routers import session_chat

    class StaleSerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-live"
        active_age_ms = 20_000.0

        async def repair_idle_queue(self):
            return False

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("stale writer should reject before queueing managed launch")

    monkeypatch.setattr(session_chat, "get_write_serializer", lambda: StaleSerializer())

    with pytest.raises(session_chat.ManagedLocalLaunchError) as exc_info:
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                SimpleNamespace(),
                SimpleNamespace(provider="codex", runner_target="cinder"),
            )
        )

    assert exc_info.value.status_code == 503
    assert "database writer is stalled" in exc_info.value.detail


def test_this_device_launch_builds_response_inside_serialized_write(monkeypatch):
    from zerg.routers import session_chat

    request_db = object()
    write_db = object()
    expected_result = object()
    expected_response = object()
    seen: dict[str, object | int | None] = {}

    class FreshSessionSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0

        async def repair_idle_queue(self):
            return False

        async def execute_or_direct(self, fn, fallback_db, **kwargs):
            seen["fallback_db"] = fallback_db
            seen["kwargs"] = kwargs
            return fn(write_db)

    def fake_response(db, result, *, owner_id=None):
        seen["response_db"] = db
        seen["response_result"] = result
        seen["owner_id"] = owner_id
        return expected_response

    monkeypatch.setattr(session_chat, "get_write_serializer", lambda: FreshSessionSerializer())
    monkeypatch.setattr(session_chat, "launch_managed_local_session_sync", lambda db, _params: expected_result)
    monkeypatch.setattr(session_chat, "_managed_local_launch_response", fake_response)

    result = asyncio.run(
        session_chat._launch_managed_local_session_serialized(
            request_db,
            SimpleNamespace(owner_id=42, provider="codex", runner_target="cinder"),
        )
    )

    assert result == (expected_result, expected_response)
    assert seen["fallback_db"] is request_db
    assert seen["response_db"] is write_db
    assert seen["response_result"] is expected_result
    assert seen["owner_id"] == 42
    assert seen["kwargs"] == {"label": "managed-launch", "auto_commit": False}


def test_this_device_launch_does_not_block_event_loop(monkeypatch):
    from zerg.routers import session_chat

    expected_result = object()
    expected_response = object()

    class ThreadedSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0

        async def repair_idle_queue(self):
            return False

        async def execute_or_direct(self, fn, fallback_db, **_kwargs):
            return await asyncio.to_thread(fn, fallback_db)

    def fake_launch(_db, _params):
        time.sleep(0.2)
        return expected_result

    async def run_probe():
        monkeypatch.setattr(session_chat, "get_write_serializer", lambda: ThreadedSerializer())
        monkeypatch.setattr(session_chat, "launch_managed_local_session_sync", fake_launch)
        monkeypatch.setattr(
            session_chat,
            "_managed_local_launch_response",
            lambda _db, result, *, owner_id=None: expected_response,
        )
        task = asyncio.create_task(
            session_chat._launch_managed_local_session_serialized(
                SimpleNamespace(rollback=lambda: None),
                SimpleNamespace(owner_id=42, provider="codex", runner_target="cinder"),
            )
        )
        started_at = time.monotonic()
        await asyncio.sleep(0.02)
        assert time.monotonic() - started_at < 0.1
        result = await task
        assert result == (expected_result, expected_response)

    asyncio.run(run_probe())


def test_this_device_launch_uses_serializer_label(monkeypatch):
    from zerg.routers import session_chat

    expected_result = object()
    expected_response = object()
    calls: list[dict] = []

    class RecordingSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0

        async def repair_idle_queue(self):
            return False

        async def execute_or_direct(self, fn, fallback_db, **kwargs):
            calls.append(kwargs)
            return fn(fallback_db)

    monkeypatch.setattr(session_chat, "get_write_serializer", lambda: RecordingSerializer())
    monkeypatch.setattr(session_chat, "launch_managed_local_session_sync", lambda _db, _params: expected_result)
    monkeypatch.setattr(
        session_chat,
        "_managed_local_launch_response",
        lambda _db, result, *, owner_id=None: expected_response,
    )

    result = asyncio.run(
        session_chat._launch_managed_local_session_serialized(
            SimpleNamespace(),
            SimpleNamespace(owner_id=42, provider="codex", runner_target="cinder"),
        )
    )

    assert result == (expected_result, expected_response)
    assert calls == [{"label": "managed-launch", "auto_commit": False}]


def test_this_device_launch_rejects_claude_without_native_channels(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, _runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "claude",
                    "native_claude_channels_available": False,
                },
            )
        finally:
            api_app.dependency_overrides = {}

    assert response.status_code == 412
    assert "requires the local Claude channel bridge" in response.json()["detail"]


def test_this_device_launch_creates_native_claude_session(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "claude",
                    "project": "demo",
                    "display_name": "Demo session",
                    "native_claude_channels_available": True,
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
        runtime_state = (
            db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == payload["session_id"]).one()
        )

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == "claude_channel_bridge"
    assert payload["source_runner_id"] == runner.id
    assert payload["source_runner_name"] == "cinder"
    assert payload["managed_session_name"] == "Demo-session"
    assert payload["provider_session_id"]
    assert payload["provider_session_id"] != payload["session_id"]
    assert project_provider_session_id(db, session) == payload["provider_session_id"]
    assert f"--session-id {payload['provider_session_id']}" in payload["attach_command"]
    assert f"LONGHOUSE_PROVIDER_SESSION_ID={payload['provider_session_id']}" in payload["attach_command"]
    capabilities, control = _project_control(db, session)
    assert capabilities.managed_transport.value == "claude_channel_bridge"
    assert control.source_runner_id == runner.id
    assert runtime_state.phase == "idle"


def test_this_device_launch_uses_token_device_id_for_runner_lookup(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "claude",
                    "display_name": "Demo session",
                    "machine_name": "cinder.local",
                    "native_claude_channels_available": True,
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()

    assert response.status_code == 200, response.text
    assert payload["source_runner_name"] == "cinder"
    _capabilities, control = _project_control(db, session)
    assert control.source_runner_id == runner.id
    assert session.device_id == "cinder"


@pytest.mark.parametrize(
    ("provider", "expected_transport"),
    [
        ("claude", "claude_channel_bridge"),
        ("codex", "codex_app_server"),
        ("opencode", "opencode_server_bridge"),
        ("antigravity", "antigravity_hook_inbox"),
        ("cursor", "cursor_helm"),
    ],
)
def test_this_device_launch_response_contract_matrix(monkeypatch, tmp_path, provider, expected_transport):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, _runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )
        request_payload = {
            "cwd": "/tmp/demo",
            "provider": provider,
            "project": "demo",
        }
        if provider == "claude":
            request_payload["native_claude_channels_available"] = True

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json=request_payload,
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == expected_transport
    assert payload["source_runner_name"] == "cinder"
    assert payload["provider"] == provider

    if provider == "claude":
        assert payload["provider_session_id"]
        assert payload["provider_session_id"] != payload["session_id"]
        assert project_provider_session_id(db, session) == payload["provider_session_id"]
        assert f"--session-id {payload['provider_session_id']}" in payload["attach_command"]
        assert f"LONGHOUSE_PROVIDER_SESSION_ID={payload['provider_session_id']}" in payload["attach_command"]
    elif provider == "codex":
        assert payload["provider_session_id"] is None
        assert "codex-bridge attach --session-id" in payload["attach_command"]
        assert payload["session_id"] in payload["attach_command"]
    elif provider == "opencode":
        assert payload["provider_session_id"] is None
        assert "opencode-channel attach --session-id" in payload["attach_command"]
        assert payload["session_id"] in payload["attach_command"]
    elif provider == "antigravity":
        assert payload["provider_session_id"] is None
        assert payload["attach_command"] == ""
    elif provider == "cursor":
        assert payload["provider_session_id"] is None
        # Helm is a PTY pass-through in the user's terminal; no separate attach.
        assert payload["attach_command"] == ""


def test_this_device_launch_creates_native_codex_session(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    reset_pubsub_for_test()
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, _runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        timeline_seq = get_pubsub().peek_latest_seq(TOPIC_TIMELINE)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "codex",
                    "project": "demo",
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
        runtime_state = (
            db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == payload["session_id"]).one()
        )

    async def _next_timeline_message():
        with get_pubsub().subscribe(TOPIC_TIMELINE, since_seq=timeline_seq) as subscription:
            return await subscription.next_message(timeout=0.1)

    timeline_message = asyncio.run(_next_timeline_message())

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == "codex_app_server"
    assert payload["source_runner_id"] is None
    assert '"$engine" codex-bridge attach --session-id' in payload["attach_command"]
    capabilities, control = _project_control(db, session)
    assert capabilities.managed_transport.value == "codex_app_server"
    assert control.source_runner_id is None
    assert runtime_state.phase == "idle"
    assert timeline_message is not None
    assert timeline_message.payload["session_id"] == payload["session_id"]
    assert timeline_message.payload["kind"] == "runtime"
    assert timeline_message.payload["source"] == "managed_local_launch"
    reset_pubsub_for_test()


def test_this_device_launch_creates_native_antigravity_session(monkeypatch, tmp_path):
    from zerg.models.agents import SessionConnection
    from zerg.services import managed_local_launcher

    reset_pubsub_for_test()
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(
            managed_local_launcher,
            "get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda *_args: True),
        )

        try:
            response = client.post(
                "/api/sessions/managed-local/this-device",
                json={
                    "cwd": "/tmp/demo",
                    "provider": "antigravity",
                    "project": "demo",
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()
        session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
        runtime_state = (
            db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == payload["session_id"]).one()
        )
        connection = db.query(SessionConnection).one()

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == "antigravity_hook_inbox"
    assert payload["source_runner_id"] is None
    assert payload["attach_command"] == ""
    assert session.provider == "antigravity"
    capabilities, control = _project_control(db, session)
    assert capabilities.managed_transport.value == "antigravity_hook_inbox"
    assert control.source_runner_id is None
    assert runtime_state.phase == "idle"
    assert connection.control_plane == "antigravity_hook_inbox"
    assert connection.can_tail_output == 1
    assert connection.can_send_input == 1
    assert connection.can_interrupt == 0
    reset_pubsub_for_test()
