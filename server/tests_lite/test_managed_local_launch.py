from __future__ import annotations

import asyncio
import os
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

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
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.database import make_live_write_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.live_archive_outbox import MANAGED_LOCAL_LAUNCH_KIND
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import _derive_project
from zerg.services.managed_local_launcher import _initial_provider_session_id_for_spawn
from zerg.services.managed_local_launcher import build_managed_local_launch_plan
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


@pytest.fixture(autouse=True)
def managed_launch_live_store(monkeypatch, tmp_path):
    import zerg.database as database_module
    from zerg.routers import session_chat

    live_url = f"sqlite:///{tmp_path / 'managed-launch-live-autouse.db'}"
    live_engine = make_live_engine(live_url)
    live_write_engine = make_live_write_engine(live_url)
    initialize_live_database(live_engine)
    LiveSession = make_sessionmaker(live_engine)
    LiveWriteSession = make_sessionmaker(live_write_engine)

    class LiveSerializer:
        is_configured = True

        async def execute(self, fn, **_kwargs):
            with LiveWriteSession() as live_db:
                result = fn(live_db)
                live_db.commit()
                return result

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(session_chat, "get_live_write_serializer", lambda: LiveSerializer())
    try:
        yield LiveSession
    finally:
        live_engine.dispose()
        live_write_engine.dispose()


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


def test_managed_local_launch_plan_builds_codex_attach_command_without_archive_db():
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=1,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="codex",
            project="demo",
            machine_name="cinder",
        )
    )

    assert plan.provider == "codex"
    assert str(plan.session_id) in plan.attach_command
    assert "codex-bridge attach --session-id" in plan.attach_command
    assert plan.provider_session_id is None
    assert plan.source_name == "cinder"
    assert plan.project == "demo"
    assert plan.managed_transport == "codex_app_server"


def test_managed_local_launch_plan_builds_claude_attach_command_without_archive_db():
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=1,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="claude",
            project="demo",
            machine_name="cinder",
            native_claude_channels_available=True,
        )
    )

    assert plan.provider == "claude"
    assert plan.provider_session_id
    assert plan.provider_session_id in plan.attach_command
    assert "LONGHOUSE_PROVIDER_SESSION_ID" in plan.attach_command
    assert str(plan.session_id) in plan.attach_command
    assert plan.managed_transport == "claude_channel_bridge"


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

    client = TestClient(app, backend="asyncio")
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

    assert response.status_code == 200, response.text
    assert payload["source_runner_id"] == runner.id
    assert payload["source_runner_name"] == "cinder"
    assert runner.name == "cinder"


def test_this_device_launch_uses_machine_name_as_dev_device_id(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, runner = _seed_user_and_runner(db)
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

    assert response.status_code == 200, response.text
    assert payload["source_runner_id"] == runner.id
    assert payload["source_runner_name"] == "cinder"
    assert payload["provider"] == "antigravity"


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

    assert response.status_code == 200, response.text
    assert payload["source_runner_id"] is None
    assert payload["source_runner_name"] == "cinder"
    assert payload["managed_transport"] == "codex_app_server"


def test_this_device_launch_returns_client_minted_session_id_unchanged(monkeypatch, tmp_path):
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)
    minted = uuid4()

    with SessionLocal() as db:
        user, _runner = _seed_user_and_runner(db)
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
                    "provider": "cursor",
                    "project": "demo",
                    "session_id": str(minted),
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()

    assert response.status_code == 200, response.text
    assert payload["session_id"] == str(minted)


def test_this_device_launch_uses_live_store_not_archive_writer(monkeypatch, tmp_path, managed_launch_live_store):
    from zerg.routers import session_chat
    from zerg.services import managed_local_launcher

    SessionLocal = _make_db(tmp_path)
    live_calls: list[dict] = []

    class RecordingLiveSerializer:
        is_configured = True

        async def execute(self, fn, **kwargs):
            live_calls.append(kwargs)
            with managed_launch_live_store() as live_db:
                result = fn(live_db)
                live_db.commit()
                return result

    with SessionLocal() as db:
        user = User(email="managed-local@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(session_chat, "get_live_write_serializer", lambda: RecordingLiveSerializer())
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

    assert response.status_code == 200, response.text
    assert live_calls == [{"label": "managed-launch-readiness"}]
    assert payload["managed_transport"] == "codex_app_server"
    assert db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).count() == 0
    with managed_launch_live_store() as live_db:
        readiness = live_db.get(LiveLaunchReadiness, payload["session_id"])
        assert readiness is not None
        assert readiness.state == "pending"


def test_this_device_launch_returns_hot_readiness_when_archive_writer_is_stale(monkeypatch, tmp_path):
    import zerg.database as database_module
    from zerg.routers import session_chat

    live_url = f"sqlite:///{tmp_path / 'managed-launch-live.db'}"
    live_engine = make_live_engine(live_url)
    live_write_engine = make_live_write_engine(live_url)
    initialize_live_database(live_engine)
    LiveSession = make_sessionmaker(live_engine)
    LiveWriteSession = make_sessionmaker(live_write_engine)

    class LiveSerializer:
        is_configured = True

        async def execute(self, fn, **_kwargs):
            with LiveWriteSession() as live_db:
                result = fn(live_db)
                live_db.commit()
                return result

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(session_chat, "get_live_write_serializer", lambda: LiveSerializer())
    monkeypatch.setattr(session_chat, "resolve_managed_local_launch_runner", lambda _db, _params: None)

    result, response = asyncio.run(
        session_chat._launch_managed_local_session_serialized(
            None,
            ManagedLocalLaunchParams(
                owner_id=42,
                runner_target="cinder",
                cwd="/tmp/demo",
                provider="codex",
                project="demo",
                machine_name="cinder",
            ),
        )
    )

    assert result is None
    assert response.provider == "codex"
    assert response.managed_transport.value == "codex_app_server"
    assert "codex-bridge attach --session-id" in response.attach_command
    assert response.session_id in response.attach_command
    with LiveSession() as live_db:
        row = live_db.get(LiveLaunchReadiness, response.session_id)
        assert row is not None
        assert row.state == "pending"
        assert row.provider == "codex"
        assert row.device_id == "cinder"
        outbox = live_db.query(LiveArchiveOutbox).one()
        assert outbox.kind == MANAGED_LOCAL_LAUNCH_KIND
        assert response.session_id in outbox.idempotency_key


@pytest.mark.parametrize(
    ("provider", "expected_transport", "expect_empty_attach"),
    [
        ("cursor", "cursor_helm", True),
        ("codex", "codex_app_server", False),
    ],
)
def test_this_device_launch_materializes_live_catalog_without_archive_db(
    monkeypatch,
    provider,
    expected_transport,
    expect_empty_attach,
):
    import zerg.database as database_module
    from pathlib import Path
    from uuid import uuid4

    from zerg.catalogd.client import CatalogClient
    from zerg.catalogd.server import CatalogDaemon
    from zerg.routers import session_chat

    root = Path("/tmp") / f"lh-ml-{provider}-{uuid4().hex[:8]}"
    root.mkdir(mode=0o700)
    database_path = root / "live.db"
    socket_path = root / "catalogd.sock"
    live_engine = make_live_engine(f"sqlite:///{database_path}")
    LiveSession = make_sessionmaker(live_engine)

    async def _run_launch():
        daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
        await daemon.start()
        client = CatalogClient(socket_path)
        monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
        monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
        monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: client)
        monkeypatch.setattr(
            session_chat,
            "get_live_write_serializer",
            lambda: (_ for _ in ()).throw(AssertionError("catalog launch must not use the API live serializer")),
        )
        try:
            return await session_chat._launch_managed_local_session_serialized(
                SimpleNamespace(),
                ManagedLocalLaunchParams(
                    owner_id=42,
                    runner_target="cinder",
                    cwd="/tmp/demo",
                    provider=provider,
                    project="demo",
                    machine_name="cinder",
                ),
            )
        finally:
            await client.close()
            await daemon.close()

    try:
        _result, response = asyncio.run(_run_launch())
        assert response.provider == provider
        assert response.managed_transport.value == expected_transport
        if expect_empty_attach:
            assert response.attach_command == ""
        else:
            assert "codex-bridge attach --session-id" in response.attach_command
            assert response.session_id in response.attach_command
        assert response.provider_session_id is None
        with LiveSession() as live_db:
            catalog = live_db.get(LiveSessionCatalog, response.session_id)
            assert catalog is not None
            assert catalog.project == "demo"
            assert catalog.primary_thread_id is not None
            assert live_db.get(LiveSessionThread, catalog.primary_thread_id) is not None
            attempt = live_db.query(LiveSessionLaunchAttempt).one()
            assert attempt.command_id == f"managed-local-{response.session_id}"
            assert attempt.state == "pending"
            run = live_db.query(LiveSessionRun).one()
            connection = live_db.query(LiveSessionConnection).one()
            assert connection.run_id == run.id
            assert connection.state == "detached"
            assert connection.device_id == "cinder"
            assert live_db.query(LiveSessionThreadAlias).count() == 0
    finally:
        live_engine.dispose()
        for path in root.iterdir():
            path.unlink(missing_ok=True)
        root.rmdir()


def test_this_device_launch_surfaces_catalog_rejection_without_retry_theater(monkeypatch):
    import zerg.database as database_module
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.protocol import CatalogRpcError
    from zerg.routers import session_chat
    from zerg.services.managed_local_launcher import ManagedLocalLaunchError

    class RejectingCatalog:
        async def call(self, method, params, **_kwargs):
            assert method == "session.launch.local.create.v2"
            raise CatalogRemoteError(
                CatalogRpcError(
                    code="invalid_request",
                    message="local launch.plan.attach_command must be a string of at most 4096 characters",
                    retryable=False,
                    retry_after_ms=None,
                    details={},
                )
            )

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: RejectingCatalog())

    with pytest.raises(ManagedLocalLaunchError) as exc_info:
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                SimpleNamespace(),
                ManagedLocalLaunchParams(
                    owner_id=42,
                    runner_target="cinder",
                    cwd="/tmp/demo",
                    provider="cursor",
                    project="demo",
                    machine_name="cinder",
                ),
            )
        )

    assert exc_info.value.status_code == 500
    assert "attach_command" in exc_info.value.detail
    assert "retry shortly" not in exc_info.value.detail.lower()
    assert "unavailable" not in exc_info.value.detail.lower()


@pytest.mark.parametrize(
    ("case", "expected_status", "detail_must_include", "detail_must_exclude"),
    [
        ("unavailable", 503, ("unavailable",), ("attach_command",)),
        ("invalid_request", 500, ("attach_command",), ("retry shortly", "unavailable")),
        ("conflict", 409, ("conflict",), ("retry shortly",)),
    ],
)
def test_managed_local_catalog_error_class_matrix(
    monkeypatch,
    case,
    expected_status,
    detail_must_include,
    detail_must_exclude,
):
    """Phase A guard: do not remap contract bugs into fake catalogd-unavailable 503s."""
    import zerg.database as database_module
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.catalogd.protocol import CatalogRpcError
    from zerg.routers import session_chat
    from zerg.services.managed_local_launcher import ManagedLocalLaunchError

    if case == "unavailable":
        raised: Exception = CatalogUnavailable("catalogd socket missing")
    elif case == "invalid_request":
        raised = CatalogRemoteError(
            CatalogRpcError(
                code="invalid_request",
                message="local launch.plan.attach_command must be a string of at most 4096 characters",
                retryable=False,
                retry_after_ms=None,
                details={},
            )
        )
    else:
        raised = CatalogRemoteError(
            CatalogRpcError(
                code="conflict",
                message="managed-local launch identity conflict",
                retryable=False,
                retry_after_ms=None,
                details={},
            )
        )

    class BoomCatalog:
        async def call(self, method, params, **_kwargs):
            raise raised

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: BoomCatalog())

    with pytest.raises(ManagedLocalLaunchError) as exc_info:
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                SimpleNamespace(),
                ManagedLocalLaunchParams(
                    owner_id=42,
                    runner_target="cinder",
                    cwd="/tmp/demo",
                    provider="cursor",
                    project="demo",
                    machine_name="cinder",
                ),
            )
        )

    assert exc_info.value.status_code == expected_status
    detail = exc_info.value.detail.lower()
    for needle in detail_must_include:
        assert needle in detail
    for needle in detail_must_exclude:
        assert needle not in detail


def test_this_device_launch_skips_runtime_pubsub_for_hot_readiness(monkeypatch, tmp_path):
    from zerg.routers import session_chat
    from zerg.services import session_pubsub
    from zerg.services.session_chat_impl import _managed_local_launch_response_from_plan

    SessionLocal = _make_db(tmp_path)
    publish_calls: list[dict] = []

    async def fake_launch(_db, params):
        plan = build_managed_local_launch_plan(params)
        return None, _managed_local_launch_response_from_plan(plan, owner_id=params.owner_id)

    with SessionLocal() as db:
        user = User(email="managed-local@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)
        device_token = SimpleNamespace(owner_id=user.id, device_id="cinder")
        client, api_app = _make_device_client(db, device_token)
        monkeypatch.setattr(session_chat, "_launch_managed_local_session_serialized", fake_launch)
        monkeypatch.setattr(
            session_pubsub,
            "publish_session_runtime_update",
            lambda **kwargs: publish_calls.append(kwargs),
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

    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "codex"
    assert publish_calls == []


def test_this_device_launch_reports_503_when_hot_readiness_write_fails(monkeypatch):
    import zerg.database as database_module
    from zerg.routers import session_chat

    class FailingLiveSerializer:
        is_configured = True

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("live db unavailable")

    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(session_chat, "get_live_write_serializer", lambda: FailingLiveSerializer())
    monkeypatch.setattr(session_chat, "resolve_managed_local_launch_runner", lambda _db, _params: None)

    with pytest.raises(session_chat.ManagedLocalLaunchError) as exc_info:
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                SimpleNamespace(),
                ManagedLocalLaunchParams(
                    owner_id=42,
                    runner_target="cinder",
                    cwd="/tmp/demo",
                    provider="codex",
                    project="demo",
                    machine_name="cinder",
                ),
            )
        )

    assert exc_info.value.status_code == 503
    assert "Live Store writer failed" in exc_info.value.detail


def test_this_device_launch_validates_response_before_hot_write(monkeypatch, managed_launch_live_store):
    from zerg.routers import session_chat
    from zerg.services import managed_local_launcher

    monkeypatch.setattr(
        managed_local_launcher,
        "_initial_provider_session_id_for_spawn",
        lambda _provider: None,
    )
    monkeypatch.setattr(session_chat, "resolve_managed_local_launch_runner", lambda _db, _params: None)

    with pytest.raises(RuntimeError, match="missing provider_session_id"):
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                SimpleNamespace(),
                ManagedLocalLaunchParams(
                    owner_id=42,
                    runner_target="cinder",
                    cwd="/tmp/demo",
                    provider="claude",
                    project="demo",
                    machine_name="cinder",
                    native_claude_channels_available=True,
                ),
            )
        )

    with managed_launch_live_store() as live_db:
        assert live_db.query(LiveLaunchReadiness).count() == 0
        assert live_db.query(LiveArchiveOutbox).count() == 0


def test_this_device_launch_uses_live_serializer_label(monkeypatch, managed_launch_live_store):
    from zerg.routers import session_chat

    calls: list[dict] = []

    class RecordingLiveSerializer:
        is_configured = True

        async def execute(self, fn, **kwargs):
            calls.append(kwargs)
            with managed_launch_live_store() as live_db:
                result = fn(live_db)
                live_db.commit()
                return result

    monkeypatch.setattr(session_chat, "get_live_write_serializer", lambda: RecordingLiveSerializer())
    monkeypatch.setattr(session_chat, "resolve_managed_local_launch_runner", lambda _db, _params: None)

    result, response = asyncio.run(
        session_chat._launch_managed_local_session_serialized(
            SimpleNamespace(),
            ManagedLocalLaunchParams(
                owner_id=42,
                runner_target="cinder",
                cwd="/tmp/demo",
                provider="codex",
                project="demo",
                machine_name="cinder",
            ),
        )
    )

    assert result is None
    assert response.provider == "codex"
    assert calls == [{"label": "managed-launch-readiness"}]


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


def test_this_device_launch_returns_native_claude_hot_launch(monkeypatch, tmp_path, managed_launch_live_store):
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
                    "project": "demo",
                    "display_name": "Demo session",
                    "native_claude_channels_available": True,
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == "claude_channel_bridge"
    assert payload["source_runner_id"] == _runner.id
    assert payload["source_runner_name"] == "cinder"
    assert payload["managed_session_name"] == "Demo-session"
    assert payload["provider_session_id"]
    assert payload["provider_session_id"] != payload["session_id"]
    assert f"--session-id {payload['provider_session_id']}" in payload["attach_command"]
    assert f"LONGHOUSE_PROVIDER_SESSION_ID={payload['provider_session_id']}" in payload["attach_command"]
    assert db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).count() == 0
    with managed_launch_live_store() as live_db:
        readiness = live_db.get(LiveLaunchReadiness, payload["session_id"])
        assert readiness is not None
        assert readiness.state == "pending"
        assert readiness.provider == "claude"
        outbox = live_db.query(LiveArchiveOutbox).one()
        assert outbox.kind == MANAGED_LOCAL_LAUNCH_KIND


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

    assert response.status_code == 200, response.text
    assert payload["source_runner_name"] == "cinder"
    assert payload["source_runner_id"] == runner.id
    assert runner.name == "cinder"


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

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == expected_transport
    assert payload["source_runner_name"] == "cinder"
    assert payload["provider"] == provider

    if provider == "claude":
        assert payload["provider_session_id"]
        assert payload["provider_session_id"] != payload["session_id"]
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


def test_this_device_launch_returns_native_codex_hot_launch(monkeypatch, tmp_path, managed_launch_live_store):
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
                    "provider": "codex",
                    "project": "demo",
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == "codex_app_server"
    assert payload["source_runner_id"] == _runner.id
    assert '"$engine" codex-bridge attach --session-id' in payload["attach_command"]
    assert db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).count() == 0
    with managed_launch_live_store() as live_db:
        readiness = live_db.get(LiveLaunchReadiness, payload["session_id"])
        assert readiness is not None
        assert readiness.state == "pending"
        outbox = live_db.query(LiveArchiveOutbox).one()
        assert outbox.kind == MANAGED_LOCAL_LAUNCH_KIND


def test_this_device_launch_returns_native_antigravity_hot_launch(monkeypatch, tmp_path):
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
                    "provider": "antigravity",
                    "project": "demo",
                },
            )
        finally:
            api_app.dependency_overrides = {}

        payload = response.json()

    assert response.status_code == 200, response.text
    assert payload["managed_transport"] == "antigravity_hook_inbox"
    assert payload["source_runner_id"] == runner.id
    assert payload["attach_command"] == ""
    assert payload["provider"] == "antigravity"
    assert db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).count() == 0
