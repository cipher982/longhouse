"""Tests for the /agents/machines and /timeline/machines directory routes.

Phase 0 of the remote-session-launch epic. See
``docs/specs/remote-session-launch.md`` and
``docs/specs/machine-control-truth.md``.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from datetime import timezone
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from sqlalchemy.orm import sessionmaker  # noqa: E402

from tests_lite.live_catalog_harness import provision_live_catalog  # noqa: E402
from zerg.database import Base  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402
from zerg.services import machine_control_operations  # noqa: E402
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
        rows = db.query(DeviceToken).filter(DeviceToken.owner_id == owner_id, DeviceToken.revoked_at.is_(None)).all()
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
    provider_readiness=None,
    websocket=None,
):
    asyncio.run(
        registry.register(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=device_id,
            engine_build="test-build",
            supports=list(supports),
            provider_readiness=provider_readiness,
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
    assert entry.control_operations_by_provider == {
        "claude": ("turn_start",),
        "codex": ("send", "turn_start"),
    }
    assert entry.engine_build == "test-build"
    assert entry.launch.blocked_by is None
    assert [option.provider for option in entry.launch.providers] == ["claude", "codex"]
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


def test_directory_exposes_proven_claude_console_adapter(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="claude-host", supports=("claude.turn_start",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1
    assert tuple(option.provider for option in entries[0].launch.providers) == ("claude",)
    assert entries[0].launch.blocked_by is None
    assert entries[0].launch.default_provider == "claude"


def test_directory_exposes_proven_opencode_console_adapter(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="opencode-host", supports=("opencode.turn_start",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1


def test_directory_reports_antigravity_send_control(tmp_path):
    """A machine advertising antigravity.send surfaces it as a control.

    This asserted the opposite between 2026-07-31 and 2026-08-20, for a good
    reason: the engine advertised antigravity.send whenever `agy` was on PATH
    and then refused every antigravity command before dispatch, so the machines
    API listed a control that always failed. The engine routes it now. What
    keeps the listing honest is no longer the absence of the advertisement but
    the per-session hook-readiness gate behind it.
    """

    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="antigravity-host", supports=("antigravity.send",))

    entries = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)

    assert len(entries) == 1
    assert entries[0].control_operations_by_provider == {"antigravity": ("send",)}
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
        "z-ready",
        "a-blocked",
        "z-blocked",
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
#
# A Runtime Host answers these routes out of the live catalog: enrollments come
# from ``machine.enrollment.list.v2``, renames from
# ``machine.enrollment.rename.v2``, and every control operation is prepared,
# reconciled and reaped inside catalogd. So the tests below run against a real
# one and authenticate with a real device token or session cookie, rather than
# stubbing auth and pointing the routes at an archive database they never read
# in production.


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


def test_agents_machines_route_matches_timeline_route():
    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")
        live.create_device_token(owner_id=owner_id, device_id="homelab")
        cookie = live.browser_cookie(owner_id=owner_id, email="owner@machines.test")
        registry = MachineControlChannelRegistry()
        _register(registry, owner_id=owner_id, device_id="cinder", supports=("codex.turn_start",))

        original, module = _swap_registry(registry)
        try:
            with live.http_client() as client:
                agents_resp = client.get("/agents/machines", headers={"X-Agents-Token": token})
                browser_resp = client.get("/timeline/machines", cookies={"longhouse_session": cookie})
        finally:
            module.get_machine_control_channel_registry = original

    assert agents_resp.status_code == 200, agents_resp.text
    assert browser_resp.status_code == 200, browser_resp.text

    # The machine surface and the browser surface read the same enrollments out
    # of the same catalog, so the bodies have to be byte-identical. last_seen_at
    # needs no normalizing: it is assigned once at register and the two calls
    # share the registry.
    assert agents_resp.json() == browser_resp.json()

    body = agents_resp.json()
    assert [m["device_id"] for m in body["machines"]] == ["cinder", "homelab"]
    assert body["machines"][0]["supports"] == ["codex.turn_start"]
    assert body["machines"][0]["control_channel_status"] == "connected"
    assert body["machines"][0]["control_operations_by_provider"] == {"codex": ["turn_start"]}
    assert body["machines"][0]["launch"] == {
        "blocked_by": None,
        "providers": [{"provider": "codex"}],
        "default_provider": "codex",
    }
    assert body["machines"][1]["online"] is False
    assert body["machines"][1]["supports"] == []
    assert body["machines"][1]["control_channel_status"] == "disconnected"
    assert body["machines"][1]["control_operations_by_provider"] == {}
    assert body["machines"][1]["launch"] == {
        "blocked_by": "control_down",
        "providers": [],
        "default_provider": None,
    }


def test_machine_rename_updates_display_name_without_changing_routing_id():
    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cube-canary")

        with live.http_client() as client:
            headers = {"X-Agents-Token": token}
            response = client.patch("/agents/machines/cube-canary", json={"machine_name": "cube"}, headers=headers)
            directory = client.get("/agents/machines", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"device_id": "cube-canary", "machine_name": "cube", "changed": True}
    assert directory.status_code == 200, directory.text
    assert directory.json()["machines"][0]["device_id"] == "cube-canary"
    assert directory.json()["machines"][0]["machine_name"] == "cube"


def test_provider_live_proof_route_dispatches_typed_machine_command():
    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")
        registry = MachineControlChannelRegistry()
        websocket = _CompletingWebSocket(registry, owner_id=owner_id, device_id="cinder")
        _register(
            registry,
            owner_id=owner_id,
            device_id="cinder",
            supports=("claude.live_proof",),
            websocket=websocket,
        )

        original, module = _swap_agents_machines_registry(registry)
        try:
            with live.http_client() as client:
                headers = {"X-Agents-Token": token}
                resp = client.post(
                    "/agents/machines/cinder/provider-live-proof",
                    json={
                        "provider": "claude",
                        "expected_provider_version": "2.1.153",
                        "run_live_token_contract": True,
                        "live_token_timeout_secs": 45,
                    },
                    headers=headers,
                )
                body = resp.json()
                status_path = str(body.get("status_url", "")).removeprefix("/api")
                running_resp = client.get(status_path, headers=headers)

                # The engine reports back over the control websocket, which
                # reconciles through catalogd rather than through any archive
                # session the API process holds.
                applied = live.rpc(
                    "control.command_result.apply.v2",
                    {
                        "owner_id": owner_id,
                        "device_id": "cinder",
                        "message": {
                            "type": "command_result",
                            "command_id": f"machine-op:{body['operation_id']}",
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
                    },
                )
                done_resp = client.get(status_path, headers=headers)
        finally:
            module.get_machine_control_channel_registry = original

    assert resp.status_code == 202, resp.text
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

    assert applied["matched"] is True
    assert done_resp.status_code == 200, done_resp.text
    done_body = done_resp.json()
    assert done_body["status"] == "succeeded"
    assert done_body["result"]["artifact"]["verdict"] == "green"


def test_provider_live_proof_operation_is_prepared_in_the_live_catalog(monkeypatch):
    """A proof operation becomes durable in catalogd before the command ships.

    This asserted the middle branch of ``_create_provider_live_proof_operation``
    until 2026-08-24: with ``live_store_configured()`` forced true it checked
    that the operation was written to a live *SQLAlchemy* store rather than the
    archive. A Runtime Host never reaches that branch, because
    ``live_catalog_enabled()`` is true whenever a live store is configured, so
    the route prepares the operation over ``machine.operation.prepare.v2``
    before either SQLAlchemy path is considered. The claim worth keeping is the
    one it was making badly: the record a status poll later reads is created in
    the catalog the Runtime Host owns, and no archive row stands in for it.
    """

    def sqlalchemy_stores_must_not_be_used(*_args, **_kwargs):
        raise AssertionError("a provider live proof must be prepared in the live catalog")

    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")
        registry = MachineControlChannelRegistry()
        websocket = _CompletingWebSocket(registry, owner_id=owner_id, device_id="cinder")
        _register(
            registry,
            owner_id=owner_id,
            device_id="cinder",
            supports=("claude.live_proof",),
            websocket=websocket,
        )

        original, module = _swap_agents_machines_registry(registry)
        monkeypatch.setattr(module, "create_provider_live_proof_operation", sqlalchemy_stores_must_not_be_used)
        monkeypatch.setattr(module, "create_live_provider_live_proof_operation", sqlalchemy_stores_must_not_be_used)
        try:
            with live.http_client() as client:
                headers = {"X-Agents-Token": token}
                resp = client.post(
                    "/agents/machines/cinder/provider-live-proof",
                    json={
                        "provider": "claude",
                        "expected_provider_version": "2.1.153",
                    },
                    headers=headers,
                )
                body = resp.json()
                running_resp = client.get(body["status_url"].removeprefix("/api"), headers=headers)
        finally:
            module.get_machine_control_channel_registry = original

        assert resp.status_code == 202, resp.text
        assert running_resp.status_code == 200, running_resp.text
        assert running_resp.json()["status"] == "running"
        assert websocket.sent[0]["command_id"] == f"machine-op:{body['operation_id']}"

        # The status route is served from catalogd, so read the row directly to
        # show the operation is durable there rather than only in the response.
        stored = live.rpc(
            "machine.operation.read.v2",
            {"owner_id": owner_id, "operation_id": body["operation_id"]},
        )
        assert stored["found"] is True
        assert stored["operation"]["command_id"] == f"machine-op:{body['operation_id']}"
        assert stored["operation"]["status"] == "running"
        assert stored["operation"]["request"]["expected_provider_version"] == "2.1.153"


def test_provider_live_proof_route_rejects_machine_without_provider_support():
    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")
        registry = MachineControlChannelRegistry()
        _register(registry, owner_id=owner_id, device_id="cinder", supports=("claude.send",))

        original, module = _swap_agents_machines_registry(registry)
        try:
            with live.http_client() as client:
                resp = client.post(
                    "/agents/machines/cinder/provider-live-proof",
                    json={"provider": "claude"},
                    headers={"X-Agents-Token": token},
                )
        finally:
            module.get_machine_control_channel_registry = original

    assert resp.status_code == 409
    assert "claude.live_proof" in resp.text


def test_provider_live_proof_route_rejects_duplicate_in_flight_request():
    """Guard: the in-flight check is catalogd's, not an archive uniqueness rule.

    Two browsers, or one impatient one, can ask for the same proof at once. On
    a Runtime Host the only thing standing between that and two engines running
    the same canary is the ``conflict`` ``machine.operation.prepare.v2`` returns
    for an active operation on the same owner, device and provider.
    """

    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")
        registry = MachineControlChannelRegistry()
        websocket = _CompletingWebSocket(registry, owner_id=owner_id, device_id="cinder")
        _register(registry, owner_id=owner_id, device_id="cinder", supports=("claude.live_proof",), websocket=websocket)

        original, module = _swap_agents_machines_registry(registry)
        try:
            with live.http_client() as client:
                headers = {"X-Agents-Token": token}
                first_resp = client.post(
                    "/agents/machines/cinder/provider-live-proof",
                    json={"provider": "claude"},
                    headers=headers,
                )
                resp = client.post(
                    "/agents/machines/cinder/provider-live-proof",
                    json={"provider": "claude"},
                    headers=headers,
                )
        finally:
            module.get_machine_control_channel_registry = original

    assert first_resp.status_code == 202, first_resp.text
    assert resp.status_code == 409
    assert "already in flight" in resp.text
    # The rejected request never reached the machine.
    assert len(websocket.sent) == 1


def test_provider_live_proof_operation_preserves_machine_error_code():
    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")
        registry = MachineControlChannelRegistry()
        websocket = _CompletingWebSocket(registry, owner_id=owner_id, device_id="cinder")
        _register(
            registry,
            owner_id=owner_id,
            device_id="cinder",
            supports=("claude.live_proof",),
            websocket=websocket,
        )

        original, module = _swap_agents_machines_registry(registry)
        try:
            with live.http_client() as client:
                headers = {"X-Agents-Token": token}
                resp = client.post(
                    "/agents/machines/cinder/provider-live-proof",
                    json={
                        "provider": "claude",
                        "expected_provider_version": "2.1.153",
                    },
                    headers=headers,
                )
                applied = live.rpc(
                    "control.command_result.apply.v2",
                    {
                        "owner_id": owner_id,
                        "device_id": "cinder",
                        "message": {
                            "type": "command_result",
                            "command_id": websocket.sent[0]["command_id"],
                            "ok": False,
                            "error": {
                                "code": "provider_version_mismatch",
                                "message": "provider live proof version mismatch",
                            },
                        },
                    },
                )
                status_resp = client.get(resp.json()["status_url"].removeprefix("/api"), headers=headers)
        finally:
            module.get_machine_control_channel_registry = original

    assert resp.status_code == 202, resp.text
    assert applied["matched"] is True
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["status"] == "failed"
    assert status_resp.json()["error"] == {
        "code": "provider_version_mismatch",
        "message": "provider live proof version mismatch",
    }


def test_machine_control_operation_route_returns_404_for_missing_operation():
    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")

        with live.http_client() as client:
            resp = client.get(f"/agents/machines/operations/{uuid4()}", headers={"X-Agents-Token": token})

    assert resp.status_code == 404


def test_machine_control_operation_route_is_owner_scoped():
    """Guard: the operation read is scoped by the token's owner, inside catalogd.

    Operation ids travel in URLs and logs. The route hands the id straight to
    ``machine.operation.read.v2``; the only thing keeping one owner from
    reading another owner's proof -- its provider, its machine, its result --
    is that the read is filtered by the owner the device token resolved to.
    """

    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        stranger_id = live.create_user("stranger@machines.test")
        owner_token = live.create_device_token(owner_id=owner_id, device_id="cinder")
        stranger_token = live.create_device_token(owner_id=stranger_id, device_id="stranger-laptop")

        operation_id = str(uuid4())
        prepared = live.rpc(
            "machine.operation.prepare.v2",
            {
                "operation_id": operation_id,
                "owner_id": owner_id,
                "device_id": "cinder",
                "provider": "claude",
                "command_type": "provider.live_proof",
                "command_id": f"machine-op:{operation_id}",
                "request_payload": {"provider": "claude"},
                "timeout_secs": 120,
            },
        )

        with live.http_client() as client:
            foreign_resp = client.get(
                f"/agents/machines/operations/{operation_id}",
                headers={"X-Agents-Token": stranger_token},
            )
            owner_resp = client.get(
                f"/agents/machines/operations/{operation_id}",
                headers={"X-Agents-Token": owner_token},
            )

    assert prepared["operation"]["operation_id"] == operation_id
    assert foreign_resp.status_code == 404
    assert owner_resp.status_code == 200, owner_resp.text
    assert owner_resp.json()["operation_id"] == operation_id


def test_machine_control_operation_route_reaps_stale_operation(monkeypatch):
    """A machine that never reports back leaves an operation the read must reap.

    Nothing sweeps operations in the background, so an engine that dies
    mid-proof would leave the status route answering "running" forever. The
    lease is materialized by the catalogd read itself.

    Only the grace period is shortened, so the lease expires in about a second
    instead of thirty-one; the expiry the route reads is still the one catalogd
    wrote, and the reaping under test is untouched.
    """

    monkeypatch.setattr(machine_control_operations, "MACHINE_OPERATION_TIMEOUT_GRACE_SECS", 0)

    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        token = live.create_device_token(owner_id=owner_id, device_id="cinder")

        operation_id = str(uuid4())
        live.rpc(
            "machine.operation.prepare.v2",
            {
                "operation_id": operation_id,
                "owner_id": owner_id,
                "device_id": "cinder",
                "provider": "claude",
                "command_type": "provider.live_proof",
                "command_id": f"machine-op:{operation_id}",
                "request_payload": {"provider": "claude"},
                "timeout_secs": 1,
            },
        )
        time.sleep(1.05)

        with live.http_client() as client:
            resp = client.get(f"/agents/machines/operations/{operation_id}", headers={"X-Agents-Token": token})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "timed_out"
    assert body["error"]["code"] == "machine_control_operation_timeout"


def test_machines_route_lists_nothing_for_an_owner_with_no_enrollments():
    # The browser directory is owner-scoped in the catalog, so a second account
    # on the same Runtime Host sees an empty list rather than the first
    # account's machines.
    with provision_live_catalog() as live:
        owner_id = live.create_user("owner@machines.test")
        live.create_device_token(owner_id=owner_id, device_id="cinder")
        stranger_id = live.create_user("stranger@machines.test")
        stranger_cookie = live.browser_cookie(owner_id=stranger_id, email="stranger@machines.test")
        registry = MachineControlChannelRegistry()
        _register(registry, owner_id=owner_id, device_id="cinder", supports=("codex.turn_start",))

        original, module = _swap_registry(registry)
        try:
            with live.http_client() as client:
                resp = client.get("/timeline/machines", cookies={"longhouse_session": stranger_cookie})
        finally:
            module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"machines": []}


def test_directory_carries_the_reason_a_provider_cannot_run(tmp_path):
    # The point of readiness: a machine that cannot run a provider says why and
    # what to do, instead of the browser learning only adapter_unavailable at
    # turn time.
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(
        registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        supports=("codex.turn_start",),
        provider_readiness={
            "claude": {"state": "not_authenticated", "remediation": "Sign in to claude on this machine"},
            "codex": {"state": "ready", "detail": "max plan"},
        },
    )

    entry = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)[0]

    assert entry.provider_readiness["claude"]["state"] == "not_authenticated"
    assert "Sign in" in entry.provider_readiness["claude"]["remediation"]
    assert entry.provider_readiness["codex"] == {"state": "ready", "detail": "max plan"}
    assert entry.to_response()["provider_readiness"]["claude"]["state"] == "not_authenticated"


def test_a_machine_that_reports_no_readiness_is_unreported_not_unready(tmp_path):
    # An engine predating readiness sends nothing. Rendering that as "no
    # provider is ready" would block launches that work today, so absence has
    # to stay absence.
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="older-engine", supports=("codex.turn_start",))

    entry = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)[0]

    assert entry.provider_readiness == {}
    assert [option.provider for option in entry.launch.providers] == ["codex"]


def test_readiness_from_a_malformed_engine_frame_is_dropped_not_trusted(tmp_path):
    # Readiness arrives from an engine the Runtime Host does not control, so a
    # junk entry must vanish rather than reach clients as a state they will try
    # to render.
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(
        registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        provider_readiness={
            "claude": "not-a-mapping",
            "codex": {"no_state_field": True},
            "opencode": {"state": "ready", "email": "someone@example.com"},
        },
    )

    entry = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)[0]

    assert "claude" not in entry.provider_readiness
    assert "codex" not in entry.provider_readiness
    # Unrecognised keys are dropped, so identity cannot ride along in readiness.
    assert entry.provider_readiness["opencode"] == {"state": "ready"}


def test_an_online_machine_reports_when_its_connection_began(tmp_path):
    # last_seen_at alone cannot tell a machine that has held one connection all
    # day from one that keeps reconnecting.
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    registry = MachineControlChannelRegistry()
    _register(registry, owner_id=OWNER_ID, device_id="cinder")

    entry = build_machines_directory(owner_id=OWNER_ID, enrollments=_enrollments(SessionLocal), registry=registry)[0]

    assert entry.connected_since is not None
    assert entry.connected_since <= entry.last_seen_at


def test_an_offline_machine_reports_neither_uptime_nor_readiness(tmp_path):
    # Both describe a live connection. Carrying last-known values forward would
    # present stale capability as current truth.
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_device_token(SessionLocal, "homelab")

    entry = build_machines_directory(
        owner_id=OWNER_ID,
        enrollments=_enrollments(SessionLocal),
        registry=MachineControlChannelRegistry(),
    )[0]

    assert entry.online is False
    assert entry.connected_since is None
    assert entry.provider_readiness == {}
