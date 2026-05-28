"""Tests for the /agents/machines and /timeline/machines directory routes.

Phase 0 of the remote-session-launch epic. See
``docs/specs/remote-session-launch.md`` and
``docs/specs/machine-control-truth.md``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
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
from zerg.database import make_engine  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_token  # noqa: E402
from zerg.dependencies.browser_auth import get_current_browser_user  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402
from zerg.services.machine_control_channel import MachineControlChannelRegistry  # noqa: E402
from zerg.services.machines_directory import build_machines_directory  # noqa: E402

OWNER_ID = 42


def _make_db(tmp_path):
    db_path = tmp_path / "test_machines_directory.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_user(SessionLocal, *, user_id: int = OWNER_ID, email: str | None = None):
    with SessionLocal() as db:
        db.add(User(id=user_id, email=email or f"user{user_id}@example.com", role="ADMIN"))
        db.commit()


def _seed_device_token(SessionLocal, device_id: str, *, owner_id: int = OWNER_ID, revoked: bool = False):
    with SessionLocal() as db:
        token = DeviceToken(
            owner_id=owner_id,
            device_id=device_id,
            token_hash=f"hash-{device_id}-{owner_id}",
        )
        if revoked:
            token.revoked_at = datetime.now(timezone.utc)
        db.add(token)
        db.commit()


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
        await self.registry.complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {
                    "provider": message["payload"]["provider"],
                    "artifact": {
                        "artifact_kind": "provider_live_canary",
                        "provider": message["payload"]["provider"],
                        "verdict": "green",
                    },
                },
            },
            owner_id=self.owner_id,
            device_id=self.device_id,
        )


class _FailingWebSocket:
    def __init__(self, registry: MachineControlChannelRegistry, *, owner_id: int, device_id: str):
        self.registry = registry
        self.owner_id = owner_id
        self.device_id = device_id

    async def send_json(self, message):
        await self.registry.complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": False,
                "error": {
                    "code": "provider_version_mismatch",
                    "message": "provider live proof version mismatch",
                },
            },
            owner_id=self.owner_id,
            device_id=self.device_id,
        )


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
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.send", "codex.launch", "claude.launch"))

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.device_id == "cinder"
    assert entry.online is True
    assert entry.control_channel_status == "connected"
    assert entry.supports == ("claude.launch", "codex.launch", "codex.send")  # sorted
    assert entry.control_operations_by_provider == {
        "codex": ("send", "launch"),
        "claude": ("launch",),
    }
    assert entry.can_launch_codex is True
    assert entry.launchable_providers == ("claude", "codex")
    assert entry.launch_blocked_by is None
    assert entry.engine_build == "test-build"


def test_directory_surfaces_offline_enrolled_machine_with_empty_supports(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "homelab")
    registry = MachineControlChannelRegistry()

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert [(e.device_id, e.online, e.supports) for e in entries] == [("homelab", False, ())]
    assert entries[0].control_channel_status == "disconnected"
    assert entries[0].control_operations_by_provider == {}
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "control_down"


def test_directory_surfaces_online_machine_without_codex_launch_as_blocked(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="old-engine", supports=("codex.send",))

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert len(entries) == 1
    assert entries[0].online is True
    assert entries[0].control_channel_status == "connected"
    assert entries[0].control_operations_by_provider == {"codex": ("send",)}
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "no_launch_support"


def test_directory_does_not_block_claude_only_launchable_machine(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="claude-host", supports=("claude.launch",))

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert len(entries) == 1
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ("claude",)
    assert entries[0].launch_blocked_by is None


def test_directory_does_not_block_opencode_only_launchable_machine(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="opencode-host", supports=("opencode.launch",))

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert len(entries) == 1
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ("opencode",)
    assert entries[0].launch_blocked_by is None


def test_directory_reports_antigravity_send_without_launchability(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="antigravity-host", supports=("antigravity.send",))

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert len(entries) == 1
    assert entries[0].control_operations_by_provider == {"antigravity": ("send",)}
    assert entries[0].can_launch_codex is False
    assert entries[0].launchable_providers == ()
    assert entries[0].launch_blocked_by == "no_launch_support"


def test_directory_prefers_online_record_over_persisted_row(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "cinder")
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert [e.device_id for e in entries] == ["cinder"]
    assert entries[0].online is True
    assert entries[0].supports == ("codex.launch",)


def test_directory_excludes_other_owners_and_revoked_tokens(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_user(SessionLocal, user_id=OWNER_ID + 1)
    _seed_device_token(SessionLocal, "someone-else", owner_id=OWNER_ID + 1)
    _seed_device_token(SessionLocal, "retired", revoked=True)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID + 1, device_id="not-mine")

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

    assert entries == []


def test_directory_sort_online_first_then_alpha(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "z-offline")
    _seed_device_token(SessionLocal, "a-offline")
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="m-online")

    with SessionLocal() as db:
        entries = build_machines_directory(db, owner_id=OWNER_ID, registry=registry)

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
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

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
    assert body["machines"][0]["supports"] == ["codex.launch"]
    assert body["machines"][0]["control_channel_status"] == "connected"
    assert body["machines"][0]["control_operations_by_provider"] == {"codex": ["launch"]}
    assert body["machines"][0]["can_launch_codex"] is True
    assert body["machines"][0]["launchable_providers"] == ["codex"]
    assert body["machines"][0]["launch_blocked_by"] is None
    assert body["machines"][1]["online"] is False
    assert body["machines"][1]["supports"] == []
    assert body["machines"][1]["control_channel_status"] == "disconnected"
    assert body["machines"][1]["control_operations_by_provider"] == {}
    assert body["machines"][1]["can_launch_codex"] is False
    assert body["machines"][1]["launchable_providers"] == []
    assert body["machines"][1]["launch_blocked_by"] == "control_down"


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
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == "cinder"
    assert body["provider"] == "claude"
    assert body["result"]["artifact"]["verdict"] == "green"
    assert len(websocket.sent) == 1
    sent = websocket.sent[0]
    assert "session_id" not in sent
    assert sent["command_type"] == "provider.live_proof"
    assert sent["payload"]["provider"] == "claude"
    assert sent["payload"]["expected_provider_version"] == "2.1.153"
    assert "timeout_secs" not in sent["payload"]
    assert "live_token_timeout_secs" not in sent["payload"]


def test_provider_live_proof_route_rejects_legacy_live_token_timeout_field(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    client, api_app = _make_agents_client(SessionLocal)
    try:
        resp = client.post(
            "/api/agents/machines/cinder/provider-live-proof",
            json={
                "provider": "claude",
                "live_token_timeout_secs": 17,
            },
        )
    finally:
        api_app.dependency_overrides.clear()

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert any(
        error.get("loc") == ["body", "live_token_timeout_secs"] and error.get("type") == "extra_forbidden"
        for error in body.get("detail", [])
    )


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
    _register(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.live_proof",))

    original, module = _swap_agents_machines_registry(registry)
    in_flight_key = (OWNER_ID, "cinder", "claude")
    module._PROVIDER_LIVE_PROOF_IN_FLIGHT.add(in_flight_key)
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
        module._PROVIDER_LIVE_PROOF_IN_FLIGHT.discard(in_flight_key)
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 409
    assert "already in flight" in resp.text


def test_provider_live_proof_route_preserves_machine_error_code(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    websocket = _FailingWebSocket(registry, owner_id=OWNER_ID, device_id="cinder")
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

    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "provider_version_mismatch",
        "message": "provider live proof version mismatch",
    }


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
