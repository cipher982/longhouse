"""Tests for the /agents/machines and /timeline/machines directory routes.

Phase 0 of the remote-session-launch epic. See
``docs/specs/remote-session-launch.md`` and
``docs/specs/machine-control-truth.md``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from zerg.database import Base  # noqa: E402
from zerg.database import get_db  # noqa: E402
from zerg.database import initialize_live_database  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_live_engine  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_token  # noqa: E402
from zerg.dependencies.browser_auth import get_current_browser_user  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402
from zerg.services.machine_control_channel import MachineControlChannelRegistry  # noqa: E402
from zerg.services.machine_control_operations import create_provider_live_proof_operation  # noqa: E402
from zerg.services.machine_control_operations import (
    reconcile_machine_control_operation_from_command_result,  # noqa: E402
)
from zerg.services.machines_directory import build_machines_directory  # noqa: E402

OWNER_ID = 42


def _make_db(tmp_path):
    db_path = tmp_path / "test_machines_directory.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_live_db(tmp_path):
    db_path = tmp_path / "test_machines_live.db"
    engine = make_live_engine(f"sqlite:///{db_path}")
    initialize_live_database(engine)
    return engine, sessionmaker(bind=engine)


class _InlineLiveSerializer:
    is_configured = True

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def execute(self, fn, *, auto_commit=True, label="", **_kwargs):
        with self.session_factory() as db:
            result = fn(db)
            if auto_commit:
                db.commit()
            return result


def _seed_user(SessionLocal, *, user_id: int = OWNER_ID, email: str | None = None):
    with SessionLocal() as db:
        db.add(User(id=user_id, email=email or f"user{user_id}@example.com", role="ADMIN"))
        db.commit()


def _seed_device_token(
    SessionLocal,
    device_id: str,
    *,
    owner_id: int = OWNER_ID,
    machine_name: str | None = None,
    revoked: bool = False,
):
    with SessionLocal() as db:
        token = DeviceToken(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=machine_name,
            token_hash=f"hash-{device_id}-{owner_id}",
        )
        if revoked:
            token.revoked_at = datetime.now(timezone.utc)
        db.add(token)
        db.commit()


def _enrollments(SessionLocal, *, owner_id: int = OWNER_ID):
    with SessionLocal() as db:
        rows = (
            db.query(DeviceToken)
            .filter(DeviceToken.owner_id == owner_id, DeviceToken.revoked_at.is_(None))
            .all()
        )
        return [
            {
                "device_id": row.device_id,
                "machine_name": row.machine_name,
                "last_used_at": row.last_used_at,
                "created_at": row.created_at,
            }
            for row in rows
        ]


class _FakeWebSocket:
    async def send_json(self, message):  # pragma: no cover — registration only
        pass


class _CompletingWebSocket:
    def __init__(self, registry: MachineControlChannelRegistry, *, owner_id: int, device_id: str):
        self.registry = registry
        self.owner_id = owner_id
        self.device_id = device_id
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


def _register(
    registry: MachineControlChannelRegistry,
    *,
    owner_id: int,
    device_id: str,
    supports=("codex.send",),
    websocket=None,
):
    asyncio.run(
        registry.register(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=device_id,
            engine_build="test-build",
            supports=list(supports),
            websocket=websocket or _FakeWebSocket(),
        )
    )


def test_directory_returns_online_machine_with_supports(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.send", "codex.turn_start", "claude.turn_start"))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.device_id == "cinder"
    assert entry.online is True
    assert entry.control_channel_status == "connected"
    assert entry.supports == ("claude.turn_start", "codex.send", "codex.turn_start")  # sorted
    assert entry.control_operations_by_provider == {"codex": ("send", "turn_start")}
    assert entry.can_launch_codex is True
    assert entry.launchable_providers == ("codex",)
    assert entry.launch_blocked_by is None
    assert entry.engine_build == "test-build"
    assert entry.launch.blocked_by is None
    assert [option.provider for option in entry.launch.providers] == ["codex"]
    assert entry.launch.default_provider == "codex"


def test_directory_surfaces_offline_enrolled_machine_with_empty_supports(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "homelab")
    registry = MachineControlChannelRegistry()

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert [(e.device_id, e.online, e.supports) for e in entries] == [("homelab", False, ())]
    assert entries[0].control_channel_status == "disconnected"
    assert entries[0].control_operations_by_provider == {}
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "control_down"
    assert entries[0].launch.providers == ()
    assert entries[0].launch.blocked_by == "control_down"


def test_directory_preserves_durable_name_while_machine_is_offline(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "cube-canary", machine_name="cube")

    entries = build_machines_directory(
        owner_id=OWNER_ID,
        enrollments=_enrollments(SessionLocal),
        registry=MachineControlChannelRegistry(),
    )

    assert entries[0].device_id == "cube-canary"
    assert entries[0].machine_name == "cube"


def test_durable_name_wins_over_connected_hello_label(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "cube-canary", machine_name="cube")
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cube-canary", supports=("codex.run_once",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert entries[0].machine_name == "cube"


def test_directory_surfaces_online_machine_without_codex_launch_as_blocked(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="old-engine", supports=("codex.send",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1
    assert entries[0].online is True
    assert entries[0].control_channel_status == "connected"
    assert entries[0].control_operations_by_provider == {"codex": ("send",)}
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "no_launch_support"


def test_directory_does_not_expose_unproven_claude_console_adapter(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="claude-host", supports=("claude.turn_start",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "no_launch_support"


def test_directory_does_not_expose_unproven_opencode_console_adapter(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="opencode-host", supports=("opencode.turn_start",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "no_launch_support"


def test_directory_reports_antigravity_send_without_launchability(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="antigravity-host", supports=("antigravity.send",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1
    assert entries[0].control_operations_by_provider == {"antigravity": ("send",)}
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "no_launch_support"
    assert entries[0].launch.blocked_by == "no_launch_support"


def test_directory_reports_cursor_native_console_turn_start(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cursor-host", supports=("cursor.turn_start",))

    entry = build_machines_directory(
        owner_id=OWNER_ID,
        enrollments=_enrollments(SessionLocal),
        registry=registry,
    )[0]

    assert entry.launchable_providers == ("cursor",)
    assert entry.launch_blocked_by is None
    assert tuple(option.provider for option in entry.launch.providers) == ("cursor",)
    assert entry.launch.blocked_by is None
    assert entry.launch.default_provider == "cursor"


def test_directory_prefers_codex_console_adapter_when_available(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(
        registry,
        owner_id=OWNER_ID,
        device_id="mixed-host",
        supports=("cursor.turn_start", "codex.turn_start"),
    )

    entry = build_machines_directory(
        owner_id=OWNER_ID,
        enrollments=_enrollments(SessionLocal),
        registry=registry,
    )[0]

    assert entry.launch.default_provider == "codex"


def test_directory_sorts_ready_then_connected_blocked_then_offline_by_name(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "z-offline")
    _seed_device_token(SessionLocal, "a-offline")
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="z-ready", supports=("claude.turn_start",))
    _register(registry, owner_id=OWNER_ID, device_id="a-ready", supports=("codex.turn_start",))
    _register(registry, owner_id=OWNER_ID, device_id="z-blocked", supports=("antigravity.send",))
    _register(registry, owner_id=OWNER_ID, device_id="a-blocked", supports=("codex.send",))

    entries = build_machines_directory(
        owner_id=OWNER_ID,
        enrollments=_enrollments(SessionLocal),
        registry=registry,
    )

    assert [entry.device_id for entry in entries] == [
        "a-ready",
        "a-blocked",
        "z-blocked",
        "z-ready",
        "a-offline",
        "z-offline",
    ]


def test_directory_prefers_online_record_over_persisted_row(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "cinder")
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.turn_start",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert [e.device_id for e in entries] == ["cinder"]
    assert entries[0].online is True
    assert entries[0].supports == ("codex.turn_start",)


def test_directory_excludes_other_owners_and_revoked_tokens(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_user(SessionLocal, user_id=OWNER_ID + 1)
    _seed_device_token(SessionLocal, "someone-else", owner_id=OWNER_ID + 1)
    _seed_device_token(SessionLocal, "retired", revoked=True)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID + 1, device_id="not-mine")

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert entries == []


def test_directory_sort_online_first_then_alpha(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "z-offline")
    _seed_device_token(SessionLocal, "a-offline")
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="m-online")

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert [e.device_id for e in entries] == ["m-online", "a-offline", "z-offline"]


# ---------- HTTP route parity ----------------------------------------------


def _make_agents_client(SessionLocal, *, owner_id: int = OWNER_ID):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(device_id="testclient", id="token-1", owner_id=owner_id)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def _make_browser_client(SessionLocal, *, owner_id: int = OWNER_ID):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_browser_user():
        return SimpleNamespace(id=owner_id, email="owner@example.com", role="ADMIN")

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_browser_user] = override_browser_user
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def _swap_registry(registry: MachineControlChannelRegistry):
    import zerg.services.machines_directory as module

    original = module.get_machine_control_channel_registry
    module.get_machine_control_channel_registry = lambda: registry
    return original, module


def _swap_agents_machines_registry(registry: MachineControlChannelRegistry):
    import zerg.routers.agents_machines as module

    original = module.get_machine_control_channel_registry
    module.get_machine_control_channel_registry = lambda: registry
    return original, module


def test_agents_machines_route_matches_timeline_route(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "homelab")
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.turn_start",))

    original, module = _swap_registry(registry)
    try:
        agents_client, api_app = _make_agents_client(SessionLocal)
        try:
            agents_resp = agents_client.get("/api/agents/machines")
        finally:
            api_app.dependency_overrides.clear()

        browser_client, api_app = _make_browser_client(SessionLocal)
        try:
            browser_resp = browser_client.get("/api/timeline/machines")
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert agents_resp.status_code == 200, agents_resp.text
    assert browser_resp.status_code == 200, browser_resp.text

    # Normalize last_seen_at since the online entry carries an assigned-at-register
    # timestamp that is identical across calls because the registry is shared.
    assert agents_resp.json() == browser_resp.json()

    body = agents_resp.json()
    assert [m["device_id"] for m in body["machines"]] == ["cinder", "homelab"]
    assert body["machines"][0]["supports"] == ["codex.turn_start"]
    assert body["machines"][0]["control_channel_status"] == "connected"
    assert body["machines"][0]["control_operations_by_provider"] == {"codex": ["turn_start"]}
    assert body["machines"][0]["can_launch_codex"] is True
    assert body["machines"][0]["launchable_providers"] == ["codex"]
    assert body["machines"][0]["launch_blocked_by"] is None
    assert body["machines"][0]["launch"] == {
        "blocked_by": None,
        "providers": [{"provider": "codex"}],
        "default_provider": "codex",
    }
    assert body["machines"][1]["online"] is False
    assert body["machines"][1]["supports"] == []
    assert body["machines"][1]["control_channel_status"] == "disconnected"
    assert body["machines"][1]["control_operations_by_provider"] == {}
    assert body["machines"][1]["can_launch_codex"] is False
    assert body["machines"][1]["launchable_providers"] == []
    assert body["machines"][1]["launch_blocked_by"] == "control_down"
    assert body["machines"][1]["launch"] == {
        "blocked_by": "control_down",
        "providers": [],
        "default_provider": None,
    }


def test_machine_rename_updates_display_name_without_changing_routing_id(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "cube-canary")
    client, api_app = _make_agents_client(SessionLocal)
    try:
        response = client.patch("/api/agents/machines/cube-canary", json={"machine_name": "cube"})
        directory = client.get("/api/agents/machines")
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"device_id": "cube-canary", "machine_name": "cube", "changed": True}
    assert directory.status_code == 200, directory.text
    assert directory.json()["machines"][0]["device_id"] == "cube-canary"
    assert directory.json()["machines"][0]["machine_name"] == "cube"


def test_provider_live_proof_route_dispatches_typed_machine_command(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    websocket = _CompletingWebSocket(registry, owner_id=OWNER_ID, device_id="cinder")
    _register(
        registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        supports=("claude.live_proof",),
        websocket=websocket,
    )

    original, module = _swap_agents_machines_registry(registry)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            resp = client.post(
                "/api/agents/machines/cinder/provider-live-proof",
                json={
                    "provider": "claude",
                    "expected_provider_version": "2.1.153",
                    "run_live_token_contract": True,
                    "live_token_timeout_secs": 45,
                },
            )
            status_url = resp.json().get("status_url")
            running_resp = client.get(status_url)
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["device_id"] == "cinder"
    assert body["provider"] == "claude"
    assert body["status"] == "running"
    assert body["operation_id"]
    assert body["status_url"] == f"/api/agents/machines/operations/{body['operation_id']}"
    assert running_resp.status_code == 200, running_resp.text
    assert running_resp.json()["status"] == "running"
    assert len(websocket.sent) == 1
    sent = websocket.sent[0]
    assert "session_id" not in sent
    assert sent["command_type"] == "provider.live_proof"
    assert sent["command_id"] == f"machine-op:{body['operation_id']}"
    assert sent["payload"]["provider"] == "claude"
    assert sent["payload"]["expected_provider_version"] == "2.1.153"
    assert sent["payload"]["run_live_token_contract"] is True
    assert sent["payload"]["live_token_timeout_secs"] == 45
    assert "timeout_secs" not in sent["payload"]

    with SessionLocal() as db:
        reconciled = reconcile_machine_control_operation_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": sent["command_id"],
                "ok": True,
                "result": {
                    "provider": "claude",
                    "artifact": {
                        "artifact_kind": "provider_live_canary",
                        "provider": "claude",
                        "verdict": "green",
                    },
                },
            },
            owner_id=OWNER_ID,
            device_id="cinder",
        )
    assert reconciled is True

    original, module = _swap_agents_machines_registry(registry)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            done_resp = client.get(body["status_url"])
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert done_resp.status_code == 200, done_resp.text
    done_body = done_resp.json()
    assert done_body["status"] == "succeeded"
    assert done_body["result"]["artifact"]["verdict"] == "green"


def test_provider_live_proof_route_uses_live_store_operation_when_configured(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    live_engine, LiveSession = _make_live_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    websocket = _CompletingWebSocket(registry, owner_id=OWNER_ID, device_id="cinder")
    _register(
        registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        supports=("claude.live_proof",),
        websocket=websocket,
    )

    def archive_operation_must_not_be_required(*_args, **_kwargs):
        raise AssertionError("provider-live operation should be created in live store")

    original, module = _swap_agents_machines_registry(registry)
    monkeypatch.setattr(module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(module.database_module, "get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr(module, "get_live_write_serializer", lambda: _InlineLiveSerializer(LiveSession))
    monkeypatch.setattr(module, "create_provider_live_proof_operation", archive_operation_must_not_be_required)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            resp = client.post(
                "/api/agents/machines/cinder/provider-live-proof",
                json={
                    "provider": "claude",
                    "expected_provider_version": "2.1.153",
                },
            )
            body = resp.json()
            running_resp = client.get(body["status_url"])
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original
        live_engine.dispose()

    assert resp.status_code == 202, resp.text
    assert running_resp.status_code == 200, running_resp.text
    assert running_resp.json()["status"] == "running"
    assert websocket.sent[0]["command_id"] == f"machine-op:{body['operation_id']}"


def test_provider_live_proof_route_rejects_machine_without_provider_support(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.launch",))

    original, module = _swap_agents_machines_registry(registry)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            resp = client.post(
                "/api/agents/machines/cinder/provider-live-proof",
                json={"provider": "claude"},
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 409
    assert "claude.live_proof" in resp.text


def test_provider_live_proof_route_rejects_duplicate_in_flight_request(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    websocket = _CompletingWebSocket(registry, owner_id=OWNER_ID, device_id="cinder")
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.live_proof",), websocket=websocket)

    original, module = _swap_agents_machines_registry(registry)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            first_resp = client.post(
                "/api/agents/machines/cinder/provider-live-proof",
                json={"provider": "claude"},
            )
            resp = client.post(
                "/api/agents/machines/cinder/provider-live-proof",
                json={"provider": "claude"},
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert first_resp.status_code == 202, first_resp.text
    assert resp.status_code == 409
    assert "already in flight" in resp.text


def test_provider_live_proof_operation_preserves_machine_error_code(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    websocket = _CompletingWebSocket(registry, owner_id=OWNER_ID, device_id="cinder")
    _register(
        registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        supports=("claude.live_proof",),
        websocket=websocket,
    )

    original, module = _swap_agents_machines_registry(registry)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            resp = client.post(
                "/api/agents/machines/cinder/provider-live-proof",
                json={
                    "provider": "claude",
                    "expected_provider_version": "2.1.153",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 202, resp.text
    command_id = websocket.sent[0]["command_id"]
    with SessionLocal() as db:
        reconciled = reconcile_machine_control_operation_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": False,
                "error": {
                    "code": "provider_version_mismatch",
                    "message": "provider live proof version mismatch",
                },
            },
            owner_id=OWNER_ID,
            device_id="cinder",
        )
    assert reconciled is True

    original, module = _swap_agents_machines_registry(registry)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            status_resp = client.get(resp.json()["status_url"])
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["status"] == "failed"
    assert status_resp.json()["error"] == {
        "code": "provider_version_mismatch",
        "message": "provider live proof version mismatch",
    }


def test_machine_control_operation_route_returns_404_for_missing_operation(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)

    client, api_app = _make_agents_client(SessionLocal)
    try:
        resp = client.get("/api/agents/machines/operations/missing-operation")
    finally:
        api_app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_machine_control_operation_route_is_owner_scoped(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_user(SessionLocal, user_id=OWNER_ID + 1)
    with SessionLocal() as db:
        operation = create_provider_live_proof_operation(
            db,
            owner_id=OWNER_ID + 1,
            device_id="cinder",
            provider="claude",
            request_payload={"provider": "claude"},
            timeout_secs=120,
        )
        operation_id = operation.id

    client, api_app = _make_agents_client(SessionLocal, owner_id=OWNER_ID)
    try:
        foreign_resp = client.get(f"/api/agents/machines/operations/{operation_id}")
    finally:
        api_app.dependency_overrides.clear()

    client, api_app = _make_agents_client(SessionLocal, owner_id=OWNER_ID + 1)
    try:
        owner_resp = client.get(f"/api/agents/machines/operations/{operation_id}")
    finally:
        api_app.dependency_overrides.clear()

    assert foreign_resp.status_code == 404
    assert owner_resp.status_code == 200, owner_resp.text
    assert owner_resp.json()["operation_id"] == operation_id


def test_machine_control_operation_route_reaps_stale_operation(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    with SessionLocal() as db:
        operation = create_provider_live_proof_operation(
            db,
            owner_id=OWNER_ID,
            device_id="cinder",
            provider="claude",
            request_payload={"provider": "claude"},
            timeout_secs=1,
        )
        operation_id = operation.id
        operation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(operation)
        db.commit()

    client, api_app = _make_agents_client(SessionLocal)
    try:
        resp = client.get(f"/api/agents/machines/operations/{operation_id}")
    finally:
        api_app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "timed_out"
    assert body["error"]["code"] == "machine_control_operation_timeout"


def test_machines_route_returns_empty_for_unknown_user(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()

    original, module = _swap_registry(registry)
    try:
        browser_client, api_app = _make_browser_client(SessionLocal, owner_id=9999)
        try:
            resp = browser_client.get("/api/timeline/machines")
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200
    assert resp.json() == {"machines": []}
