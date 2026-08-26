import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.catalogd.schema import create_catalog_engine
from zerg.models.live_store import LiveMachineControlOperation
from zerg.routers.agents_control import CONTROL_HEARTBEAT_TIMEOUT_SECS
from zerg.routers.agents_control import _control_identity
from zerg.routers.agents_control import _reconcile_console_turns_after_register
from zerg.routers.agents_control import _reconcile_machine_control_operation_result
from zerg.services.catalogd_supervisor import catalogd_paths


def _seed_running_control_operation(*, owner_id: int, device_id: str, command_id: str) -> str:
    """Leave the row a prepared control command leaves behind in the live catalog.

    ``control.command.prepare.v2`` writes exactly this row once it has resolved
    a grant; the grant resolution itself is pinned in
    ``tests_lite/test_catalogd_control_commands.py``. What the control channel
    needs is the pending operation a Machine Agent result arrives for.
    """

    database_path, _socket_path = catalogd_paths()
    engine = create_catalog_engine(database_path)
    now = datetime.now(UTC).replace(microsecond=0)
    operation_id = str(uuid4())
    try:
        with Session(engine) as db:
            db.add(
                LiveMachineControlOperation(
                    id=operation_id,
                    owner_id=owner_id,
                    session_id=str(uuid4()),
                    device_id=device_id,
                    provider="codex",
                    command_type="session.send_text",
                    command_id=command_id,
                    status="running",
                    request_json=json.dumps({"payload": {"text": "continue"}}, sort_keys=True),
                    timeout_secs=15,
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(seconds=45),
                )
            )
            db.commit()
    finally:
        engine.dispose()
    return operation_id


def _read_control_operation(operation_id: str) -> dict:
    database_path, _socket_path = catalogd_paths()
    engine = create_catalog_engine(database_path)
    try:
        with Session(engine) as db:
            operation = db.get(LiveMachineControlOperation, operation_id)
            assert operation is not None
            return {
                "status": str(operation.status),
                "result_json": operation.result_json,
                "error_json": operation.error_json,
                "finished_at": operation.finished_at,
                "expires_at": operation.expires_at,
            }
    finally:
        engine.dispose()


def test_control_heartbeat_timeout_is_a_watchdog_not_a_stale_socket_lease():
    assert 30 <= CONTROL_HEARTBEAT_TIMEOUT_SECS <= 120


def test_auth_disabled_control_channel_preserves_valid_device_token_identity():
    token = SimpleNamespace(owner_id=7, device_id="device-7")

    assert _control_identity({"device_id": "device-7"}, token, auth_disabled=True) == (7, "device-7")


def test_auth_disabled_control_channel_keeps_tokenless_dev_fallback():
    assert _control_identity({"device_id": "dev-machine"}, None, auth_disabled=True) == (0, "dev-machine")


def test_control_channel_rejects_token_device_mismatch():
    token = SimpleNamespace(owner_id=7, device_id="device-7")

    assert _control_identity({"device_id": "other-device"}, token, auth_disabled=True) is None


@pytest.mark.asyncio
async def test_control_register_reconciles_starting_console_turns(monkeypatch):
    calls = []

    async def fake_reconcile(db, *, owner_id, device_id, registry):
        calls.append((db, owner_id, device_id, registry))
        return []

    registry = object()
    monkeypatch.setattr("zerg.routers.agents_control.database_module.live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.routers.agents_control.reconcile_starting_console_turns_for_device", fake_reconcile)

    await _reconcile_console_turns_after_register(owner_id=7, device_id="cube", registry=registry)

    assert calls == [(None, 7, "cube", registry)]


@pytest.mark.asyncio
async def test_machine_control_result_reconcile_uses_write_serializer(monkeypatch):
    calls = []

    class FakeSerializer:
        async def execute_or_direct(self, fn, fallback_db, *, auto_commit, label):
            calls.append(("execute", label, fallback_db, auto_commit))
            return fn("serializer-db")

    def fake_reconcile(db, message, *, owner_id, device_id):
        calls.append(("reconcile", db, message["command_id"], owner_id, device_id))
        return True

    monkeypatch.setattr("zerg.routers.agents_control.get_write_serializer", lambda: FakeSerializer())
    monkeypatch.setattr(
        "zerg.routers.agents_control.reconcile_machine_control_operation_from_command_result",
        fake_reconcile,
    )

    matched = await _reconcile_machine_control_operation_result(
        "fallback-db",
        {"command_id": "machine-op:test"},
        owner_id=7,
        device_id="cinder",
    )

    assert matched is True
    assert calls == [
        ("execute", "machine-control-result", "fallback-db", False),
        ("reconcile", "serializer-db", "machine-op:test", 7, "cinder"),
    ]


@pytest.mark.asyncio
async def test_machine_control_result_finishes_the_operation_in_the_live_catalog(monkeypatch):
    """A Runtime Host finishes the operation over RPC, never over SQLite.

    This replaces a test that pinned the live-write-serializer branch. A live
    store is configured exactly when ``live_catalog_enabled()`` is true -- the
    live URL is the file-backed archive's sibling -- so the only process that
    reaches that branch is an archive-route child, which never serves a control
    websocket. Production reaches catalogd or it reaches nothing, and pinning
    the SQLite fallback let ``control.command_result.apply.v2`` regressions
    through. Here the daemon is real: the method has to exist, accept these
    parameters, and durably finish the operation.
    """

    def serializer_must_not_run():  # pragma: no cover - assertion is the behavior
        raise AssertionError("the live catalog reconciles control results over RPC, not over SQLite")

    monkeypatch.setattr("zerg.routers.agents_control.get_live_write_serializer", serializer_must_not_run)
    monkeypatch.setattr("zerg.routers.agents_control.get_write_serializer", serializer_must_not_run)

    with provision_live_catalog():
        command_id = f"managed-control:{uuid4()}:session.send_text"
        operation_id = _seed_running_control_operation(owner_id=7, device_id="cinder", command_id=command_id)
        other_command_id = f"managed-control:{uuid4()}:session.send_text"
        other_operation_id = _seed_running_control_operation(owner_id=8, device_id="cinder", command_id=other_command_id)

        matched = await _reconcile_machine_control_operation_result(
            None,
            {"type": "command_result", "command_id": command_id, "ok": True, "result": {"stdout": "accepted"}},
            owner_id=7,
            device_id="cinder",
        )
        stray = await _reconcile_machine_control_operation_result(
            None,
            {"type": "command_result", "command_id": f"managed-control:{uuid4()}:session.send_text", "ok": True, "result": {}},
            owner_id=7,
            device_id="cinder",
        )
        # A control channel authenticates as exactly one owner, so a result
        # naming another owner's command must not finish that owner's operation.
        cross_owner = await _reconcile_machine_control_operation_result(
            None,
            {"type": "command_result", "command_id": other_command_id, "ok": True, "result": {"stdout": "stolen"}},
            owner_id=7,
            device_id="cinder",
        )
        operation = _read_control_operation(operation_id)
        other_operation = _read_control_operation(other_operation_id)

    assert matched is True
    assert operation["status"] == "succeeded"
    assert json.loads(operation["result_json"]) == {"stdout": "accepted"}
    assert operation["error_json"] is None
    assert operation["finished_at"] is not None
    assert operation["expires_at"] is None

    assert stray is False
    assert cross_owner is False
    assert other_operation["status"] == "running"
    assert other_operation["result_json"] is None


@pytest.mark.asyncio
async def test_machine_control_result_reconcile_uses_catalogd_without_db(monkeypatch):
    calls = []

    class CatalogClient:
        async def call(self, method, params, *, timeout_seconds):
            calls.append((method, params, timeout_seconds))
            return {"matched": True, "match_kind": "operation", "commit_seq": "9"}

    def fail_serializer():  # pragma: no cover - assertion is the behavior
        raise AssertionError("catalog control reconciliation must not resolve a SQLite serializer")

    monkeypatch.setattr("zerg.routers.agents_control.database_module.live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.routers.agents_control.get_catalogd_client", lambda: CatalogClient())
    monkeypatch.setattr("zerg.routers.agents_control.get_live_write_serializer", fail_serializer)
    monkeypatch.setattr("zerg.routers.agents_control.get_write_serializer", fail_serializer)

    message = {"type": "command_result", "command_id": "machine-op:test", "ok": True, "result": {}}
    matched = await _reconcile_machine_control_operation_result(
        None,
        message,
        owner_id=7,
        device_id="cinder",
    )

    assert matched is True
    assert calls == [
        (
            "control.command_result.apply.v2",
            {"owner_id": 7, "device_id": "cinder", "message": message},
            2.0,
        )
    ]
