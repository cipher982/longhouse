from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.services.managed_control_dispatcher as dispatcher_module
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.schema import create_catalog_engine
from zerg.models.live_store import LiveMachineControlOperation
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services import catalogd_supervisor
from zerg.services.live_session_dispatch import supports_live_text_dispatch_metadata
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_ANSWER_PAUSE
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_INTERRUPT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_STEER_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_TERMINATE
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_NONE
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
from zerg.services.managed_control_dispatcher import _engine_command_id
from zerg.services.managed_control_dispatcher import dispatch_managed_control_command
from zerg.services.managed_control_dispatcher import select_managed_control_transport
from zerg.services.managed_provider_contracts import contract_for_provider


def _session(**overrides):
    values = {
        "id": uuid4(),
        "device_id": "cinder",
        "provider": "codex",
        "execution_home": "managed_local",
        "managed_transport": "codex_app_server",
        "source_runner_id": 17,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@dataclass(frozen=True)
class _SeededControlLease:
    """The lease catalogd resolves a command against."""

    run_id: UUID
    catalog_connection_id: int
    adapter_connection_id: str
    lease_generation: str


def _seed_live_control_lease(
    database_path: Path,
    *,
    session_id: UUID,
    device_id: str = "cinder",
    provider: str = "codex",
) -> _SeededControlLease:
    """Leave in the live catalog what a Helm launch leaves there.

    ``control.command.prepare.v2`` refuses a command unless all three pieces are
    present: an open run on the primary thread, an attached connection carrying
    the adapter's own identity, and the control fact that identity published.
    Seeding is direct rather than driven through a heartbeat because the
    dispatcher is what is under test here, not evidence ingest.
    """

    now = datetime.now(UTC).replace(microsecond=0)
    thread_id = uuid4()
    run_id = uuid4()
    adapter_connection_id = str(uuid4())
    lease_generation = str(uuid4())
    engine = create_catalog_engine(database_path)
    try:
        with Session(engine) as db:
            db.add(
                LiveSessionCatalog(
                    session_id=str(session_id),
                    provider=provider,
                    environment="production",
                    device_id=device_id,
                    started_at=now,
                    primary_thread_id=str(thread_id),
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                LiveSessionThread(
                    id=str(thread_id),
                    session_id=str(session_id),
                    provider=provider,
                    branch_kind="root",
                    is_primary=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                LiveSessionRun(
                    id=str(run_id),
                    thread_id=str(thread_id),
                    provider=provider,
                    host_id=device_id,
                    launch_origin="longhouse_spawned",
                    started_at=now,
                )
            )
            lease = LiveSessionConnection(
                run_id=str(run_id),
                adapter_connection_id=adapter_connection_id,
                lease_generation=lease_generation,
                control_plane=contract_for_provider(provider).control_plane,
                acquisition_kind="spawned_control",
                state="attached",
                device_id=device_id,
                can_send_input=1,
                can_interrupt=1,
                can_terminate=1,
                acquired_at=now,
                last_health_at=now,
            )
            db.add(lease)
            db.commit()
            catalog_connection_id = int(lease.id)

        value = {
            "authority_class": "provider_control",
            "provider": provider,
            "session_id": str(session_id),
            "run_id": str(run_id),
            "connection_id": adapter_connection_id,
            "lease_generation": lease_generation,
            # Only what this provider's adapter can actually publish, so a
            # maintenance-tier provider cannot pass on a grant it never issues.
            "granted_operations": [
                operation for operation in ("interrupt", "send_input", "terminate") if getattr(contract_for_provider(provider), operation)
            ],
            "state": "attached",
            "lease_ttl_ms": 900_000,
            "source": f"{provider}_control_scan",
            "observed_at": now.isoformat(),
        }
        fact = ReducerFact(
            family="control",
            subject_key=f"connection:{adapter_connection_id}:{lease_generation}",
            source=f"{provider}_control_scan",
            source_epoch=lease_generation,
            source_seq=None,
            dedupe_key=canonical_evidence_hash({**value, "dedupe": now.isoformat()}),
            evidence_hash=canonical_evidence_hash(value),
            value=value,
            observed_at=now,
            session_id=str(session_id),
        )
        with engine.begin() as connection:
            reduce_fact_batch(connection, [fact], received_at=now)
    finally:
        engine.dispose()

    return _SeededControlLease(
        run_id=run_id,
        catalog_connection_id=catalog_connection_id,
        adapter_connection_id=adapter_connection_id,
        lease_generation=lease_generation,
    )


def _seed_lease_for_new_session(*, provider: str = "codex", device_id: str = "cinder") -> tuple[UUID, _SeededControlLease]:
    """Give one fresh session a controllable lease in the running live catalog."""

    database_path, _socket_path = catalogd_supervisor.catalogd_paths()
    session_id = uuid4()
    lease = _seed_live_control_lease(database_path, session_id=session_id, provider=provider, device_id=device_id)
    return session_id, lease


def _expected_control_grant(lease: _SeededControlLease) -> dict[str, object]:
    """The grant catalogd resolves from the seeded lease and puts on the frame."""

    return {
        "connection_id": lease.adapter_connection_id,
        "catalog_connection_id": lease.catalog_connection_id,
        "run_id": str(lease.run_id),
        "lease_generation": lease.lease_generation,
        "identity_source": "adapter_bound",
    }


def _read_control_operation(database_path: Path, *, command_id: str) -> dict[str, object]:
    """Read the operation catalogd durably recorded for one command."""

    engine = create_catalog_engine(database_path)
    try:
        with Session(engine) as db:
            operation = db.query(LiveMachineControlOperation).filter(LiveMachineControlOperation.command_id == command_id).one()
            return {
                "operation_id": str(operation.id),
                "owner_id": operation.owner_id,
                "session_id": str(operation.session_id),
                "device_id": operation.device_id,
                "provider": operation.provider,
                "command_type": operation.command_type,
                "status": operation.status,
                "request": json.loads(operation.request_json),
                "result": json.loads(operation.result_json) if operation.result_json else None,
                "error": json.loads(operation.error_json) if operation.error_json else None,
            }
    finally:
        engine.dispose()


class _FakeMachineWebSocket:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)


async def _clear_machine_registry():
    await get_machine_control_channel_registry().clear_for_tests()


@pytest.fixture(autouse=True)
def _reset_machine_registry():
    asyncio.run(_clear_machine_registry())
    yield
    asyncio.run(_clear_machine_registry())


async def _connect_fake_engine(*, owner_id: int = 42, supports: list[str] | None = None) -> _FakeMachineWebSocket:
    websocket = _FakeMachineWebSocket()
    await get_machine_control_channel_registry().register(
        owner_id=owner_id,
        device_id="cinder",
        machine_name="cinder",
        engine_build="abc123",
        supports=supports or ["codex.send"],
        websocket=websocket,
    )
    return websocket


async def _complete_first_machine_command(websocket: _FakeMachineWebSocket, result, *, timeout_secs: float = 10.0):
    # Bounded by time rather than by a fixed number of yields: a dispatch that
    # first reserves the operation in a real catalogd does socket I/O before it
    # writes the frame, and twenty `sleep(0)` yields elapse long before that.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    while not websocket.sent:
        if loop.time() >= deadline:
            raise AssertionError("expected a machine control command frame")
        await asyncio.sleep(0.005)
    command_id = str(websocket.sent[0]["command_id"])
    await get_machine_control_channel_registry().complete_command(
        {
            "type": "command_result",
            "command_id": command_id,
            **result,
        }
    )


def test_select_managed_control_transport_requires_engine_channel_even_with_runner_metadata():
    assert (
        select_managed_control_transport(
            _session(source_runner_id=17),
            owner_id=42,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        )
        is None
    )


def test_engine_command_id_is_stable_and_bounded_for_long_request_ids():
    session = _session()
    first = _engine_command_id(
        session=session,
        command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        request_id="request-" + "x" * 200,
        run_id=None,
    )
    second = _engine_command_id(
        session=session,
        command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        request_id="request-" + "x" * 200,
        run_id=None,
    )
    assert first == second
    assert first is not None
    assert len(first) <= 96
    assert first.startswith(f"managed-control:{session.id}:")


def test_select_managed_control_transport_returns_none_without_engine_channel():
    assert select_managed_control_transport(_session(source_runner_id=None)) is None


def test_select_managed_control_transport_uses_engine_channel_when_supported():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            assert (
                select_managed_control_transport(
                    _session(source_runner_id=17),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_supports_claude_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["claude.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="claude",
                        managed_transport="claude_channel_bridge",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_requires_engine_channel_for_pause_answers():
    assert (
        select_managed_control_transport(
            _session(provider="claude", managed_transport="claude_channel_bridge", source_runner_id=17),
            owner_id=42,
            command_type=MANAGED_CONTROL_COMMAND_ANSWER_PAUSE,
        )
        is None
    )


def test_select_managed_control_transport_supports_claude_pause_answer_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["claude.answer_pause"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="claude",
                        managed_transport="claude_channel_bridge",
                        source_runner_id=17,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_ANSWER_PAUSE,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_supports_opencode_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["opencode.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="opencode",
                        managed_transport="opencode_server_bridge",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_supports_opencode_interrupt_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["opencode.interrupt"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="opencode",
                        managed_transport="opencode_server_bridge",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_INTERRUPT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_routes_antigravity_send_to_the_engine():
    """Antigravity send resolves to the engine channel.

    The hook-inbox path is routed and the Helm launcher seeds the control
    identity authorization binds against, so the capability resolves and the
    dispatcher hands the command to the engine like any other provider.
    """

    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["antigravity.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="antigravity",
                        managed_transport="antigravity_hook_inbox",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == "engine_channel"
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_rejects_antigravity_process_transport():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["antigravity.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="antigravity",
                        managed_transport="antigravity_process",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                is None
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "command_type",
    [MANAGED_CONTROL_COMMAND_INTERRUPT, MANAGED_CONTROL_COMMAND_STEER_TEXT],
)
def test_select_managed_control_transport_rejects_antigravity_non_send_commands(command_type):
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(
                owner_id=42,
                supports=["antigravity.send", "antigravity.interrupt", "antigravity.steer"],
            )
            assert (
                select_managed_control_transport(
                    _session(
                        provider="antigravity",
                        managed_transport="antigravity_hook_inbox",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=command_type,
                )
                is None
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_has_no_transport_without_engine_channel():
    result = asyncio.run(
        dispatch_managed_control_command(
            db=object(),
            owner_id=42,
            session=_session(source_runner_id=23),
            timeout_secs=9,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        )
    )

    assert result.ok is False
    assert result.transport == MANAGED_CONTROL_TRANSPORT_NONE
    assert result.error == MANAGED_CONTROL_UNAVAILABLE_ERROR


def test_dispatch_managed_control_command_uses_engine_channel_when_connected(live_catalog):  # noqa: F811
    session_id, lease = _seed_lease_for_new_session()

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            session = _session(id=session_id, source_runner_id=None)
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {"exit_code": 0, "stdout": "accepted"},
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
                request_id="req-123",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {"stdout": "accepted", "exit_code": 0, "stderr": ""}
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_SEND_TEXT
            assert websocket.sent[0]["payload"] == {
                "provider": "codex",
                "text": "continue",
                "longhouse_control_grant": _expected_control_grant(lease),
            }
            assert websocket.sent[0]["command_id"] == f"managed-control:{session.id}:session.send_text:req-123"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_records_the_operation_in_the_live_catalog(live_catalog):  # noqa: F811
    """A dispatch leaves one durable operation behind, written by catalogd.

    This used to stand up a private live-store database and an inline write
    serializer, and assert the API process wrote the operation row itself. That
    write only happens when ``live_catalog_enabled()`` is false, which on a
    Runtime Host it never is, so the assertion described a branch production
    does not take. On the live catalog the operation is reserved by
    ``control.command.prepare.v2`` before the frame goes out and finished by
    ``control.operation.finish.v2`` when the engine answers -- a write the API
    process never performs itself. Same claim, real path: one daemon, one lease,
    and the row read back out of the live catalog.
    """

    database_path, _socket_path = catalogd_supervisor.catalogd_paths()
    session_id = uuid4()
    lease = _seed_live_control_lease(database_path, session_id=session_id)
    session = _session(id=session_id, source_runner_id=None)
    command_id = f"managed-control:{session_id}:session.send_text:req-live-catalog"

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {"exit_code": 0, "stdout": "accepted", "stderr": ""},
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=15,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
                request_id="req-live-catalog",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert websocket.sent[0]["command_id"] == command_id
            # catalogd resolved this grant from the seeded lease; the test never
            # tells the dispatcher what identity to carry.
            assert websocket.sent[0]["payload"]["longhouse_control_grant"] == {
                "connection_id": lease.adapter_connection_id,
                "catalog_connection_id": lease.catalog_connection_id,
                "run_id": str(lease.run_id),
                "lease_generation": lease.lease_generation,
                "identity_source": "adapter_bound",
            }
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())

    operation = _read_control_operation(database_path, command_id=command_id)
    assert operation["owner_id"] == 42
    assert operation["session_id"] == str(session_id)
    assert operation["device_id"] == "cinder"
    assert operation["provider"] == "codex"
    assert operation["command_type"] == MANAGED_CONTROL_COMMAND_SEND_TEXT
    assert operation["status"] == "succeeded"
    assert operation["result"] == {"exit_code": 0, "stdout": "accepted", "stderr": ""}
    assert operation["error"] is None
    assert operation["request"]["payload"] == {"provider": "codex", "text": "continue"}

    # The route that serves this operation reads it back through catalogd, so
    # the record has to be owner-scoped there and not merely present on disk.
    served = live_catalog.rpc("machine.operation.read.v2", {"owner_id": 42, "operation_id": operation["operation_id"]})
    assert served["found"] is True
    assert served["operation"]["status"] == "succeeded"
    assert served["operation"]["result"] == {"exit_code": 0, "stdout": "accepted", "stderr": ""}
    other_owner = live_catalog.rpc("machine.operation.read.v2", {"owner_id": 43, "operation_id": operation["operation_id"]})
    assert other_owner["found"] is False


def test_catalog_managed_control_uses_catalogd_for_grant_and_operation(monkeypatch):
    calls = []

    class _CatalogClient:
        async def call(self, method, params, **_kwargs):
            calls.append((method, params))
            if method == "control.command.prepare.v2":
                return {
                    "allowed": True,
                    "operation_id": params["operation_id"],
                    "grant": {
                        "connection_id": "adapter-17",
                        "catalog_connection_id": 17,
                        "run_id": str(uuid4()),
                        "lease_generation": "adapter-lease",
                        "identity_source": "adapter_bound",
                    },
                }
            if method == "control.operation.finish.v2":
                return {"found": True, "changed": True, "commit_seq": "2"}
            raise AssertionError(method)

    monkeypatch.setattr(dispatcher_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.get_catalogd_client",
        lambda: _CatalogClient(),
    )
    # This used to also assert the API process never opened the live write
    # serializer itself. That inline write only existed on the legacy branch;
    # the dispatcher no longer imports a serializer at all, so the only way to
    # reserve and finish an operation is the catalogd round trip asserted below.

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            session = _session(source_runner_id=None)
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {"exit_code": 0, "stdout": "accepted", "stderr": ""},
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=15,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
                request_id="req-catalog",
            )
            await completer
            assert result.ok is True
            assert [method for method, _params in calls] == [
                "control.command.prepare.v2",
                "control.operation.finish.v2",
            ]
            grant = websocket.sent[0]["payload"]["longhouse_control_grant"]
            assert grant["connection_id"] == "adapter-17"
            assert grant["catalog_connection_id"] == 17
            assert grant["lease_generation"] == "adapter-lease"
            assert grant["identity_source"] == "adapter_bound"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_catalog_managed_control_normalizes_pre_identity_replay(monkeypatch):
    calls = []
    replay_run_id = str(uuid4())

    class _CatalogClient:
        async def call(self, method, params, **_kwargs):
            calls.append((method, params))
            if method == "control.command.prepare.v2":
                return {
                    "allowed": True,
                    "operation_id": params["operation_id"],
                    "exact_replay": True,
                    "grant": {
                        "connection_id": 17,
                        "run_id": replay_run_id,
                        "lease_generation": "17:legacy-lease",
                    },
                }
            if method == "control.operation.finish.v2":
                return {"found": True, "changed": True, "commit_seq": "2"}
            raise AssertionError(method)

    monkeypatch.setattr(dispatcher_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: _CatalogClient())

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            session = _session(source_runner_id=None)
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {"ok": True, "result": {"exit_code": 0, "stdout": "accepted", "stderr": ""}},
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=15,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
                request_id="req-legacy-replay",
            )
            await completer
            assert result.ok is True
            assert websocket.sent[0]["payload"]["longhouse_control_grant"] == {
                "catalog_connection_id": 17,
                "connection_id": 17,
                "run_id": replay_run_id,
                "lease_generation": "17:legacy-lease",
                "identity_source": "legacy_synthetic",
            }
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_routes_opencode_send_over_engine_channel(live_catalog):  # noqa: F811
    session_id, lease = _seed_lease_for_new_session(provider="opencode")

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["opencode.send"])
            session = _session(
                id=session_id,
                provider="opencode",
                managed_transport="opencode_server_bridge",
                source_runner_id=None,
            )
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "provider": "opencode",
                            "transport": "opencode_server_bridge",
                            "provider_session_id": "ses_test",
                        },
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "hello from browser"},
                request_id="req-opencode-send",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "opencode",
                "transport": "opencode_server_bridge",
                "provider_session_id": "ses_test",
            }
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_SEND_TEXT
            assert websocket.sent[0]["payload"] == {
                "provider": "opencode",
                "text": "hello from browser",
                "longhouse_control_grant": _expected_control_grant(lease),
            }
            assert websocket.sent[0]["command_id"] == f"managed-control:{session.id}:session.send_text:req-opencode-send"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_routes_opencode_interrupt_over_engine_channel(live_catalog):  # noqa: F811
    session_id, lease = _seed_lease_for_new_session(provider="opencode")

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["opencode.interrupt"])
            session = _session(
                id=session_id,
                provider="opencode",
                managed_transport="opencode_server_bridge",
                source_runner_id=None,
            )
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "provider": "opencode",
                            "transport": "opencode_server_bridge",
                            "provider_session_id": "ses_test",
                        },
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_INTERRUPT,
                request_id="req-opencode-interrupt",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "opencode",
                "transport": "opencode_server_bridge",
                "provider_session_id": "ses_test",
            }
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_INTERRUPT
            assert websocket.sent[0]["payload"] == {
                "provider": "opencode",
                "longhouse_control_grant": _expected_control_grant(lease),
            }
            assert websocket.sent[0]["command_id"] == (f"managed-control:{session.id}:session.interrupt:req-opencode-interrupt")
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_routes_opencode_terminate_over_engine_channel(live_catalog):  # noqa: F811
    session_id, lease = _seed_lease_for_new_session(provider="opencode")

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["opencode.terminate"])
            session = _session(
                id=session_id,
                provider="opencode",
                managed_transport="opencode_server_bridge",
                source_runner_id=None,
            )
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "provider": "opencode",
                            "transport": "opencode_server_bridge",
                            "pid": 1234,
                            "stopped": True,
                        },
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_TERMINATE,
                request_id="req-opencode-terminate",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "opencode",
                "transport": "opencode_server_bridge",
                "pid": 1234,
                "stopped": True,
            }
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_TERMINATE
            assert websocket.sent[0]["payload"] == {
                "provider": "opencode",
                "longhouse_control_grant": _expected_control_grant(lease),
            }
            assert websocket.sent[0]["command_id"] == (f"managed-control:{session.id}:session.terminate:req-opencode-terminate")
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_sends_antigravity_to_the_engine(live_catalog):  # noqa: F811
    """The send must reach the engine as a real frame."""

    session_id, lease = _seed_lease_for_new_session(provider="antigravity")

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["antigravity.send"])
            session = _session(
                id=session_id,
                provider="antigravity",
                managed_transport="antigravity_hook_inbox",
                source_runner_id=None,
            )
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "provider": "antigravity",
                            "transport": "antigravity_hook_inbox",
                        },
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
                request_id="req-agy",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert websocket.sent, "the send must reach the engine as a real frame"
            assert websocket.sent[0]["payload"]["longhouse_control_grant"] == _expected_control_grant(lease)
            assert result.data["transport"] == "antigravity_hook_inbox"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_rejects_malformed_engine_success(live_catalog):  # noqa: F811
    session_id, _lease = _seed_lease_for_new_session()

    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {"stdout": "accepted"},
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=_session(id=session_id, source_runner_id=None),
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
            )
            await completer

            assert result.ok is False
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.error == "Machine Agent control command returned malformed result"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_live_text_dispatch_metadata_accepts_engine_channel_without_runner_metadata():
    async def _run():
        await _connect_fake_engine(owner_id=42, supports=["codex.send"])
        assert (
            supports_live_text_dispatch_metadata(
                _session(source_runner_id=None),
                owner_id=42,
            )
            is True
        )

    asyncio.run(_run())


def test_live_text_dispatch_metadata_accepts_claude_engine_channel_without_runner_metadata():
    async def _run():
        await _connect_fake_engine(owner_id=42, supports=["claude.send"])
        assert (
            supports_live_text_dispatch_metadata(
                _session(provider="claude", managed_transport="claude_channel_bridge", source_runner_id=None),
                owner_id=42,
            )
            is True
        )

    asyncio.run(_run())


def test_live_text_dispatch_metadata_accepts_opencode_engine_channel_without_runner_metadata():
    async def _run():
        await _connect_fake_engine(owner_id=42, supports=["opencode.send"])
        assert (
            supports_live_text_dispatch_metadata(
                _session(provider="opencode", managed_transport="opencode_server_bridge", source_runner_id=None),
                owner_id=42,
            )
            is True
        )

    asyncio.run(_run())
