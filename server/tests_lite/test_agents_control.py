import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers.agents_control import CONTROL_HEARTBEAT_TIMEOUT_SECS
from zerg.routers.agents_control import _reconcile_machine_control_operation_result


def test_control_heartbeat_timeout_is_a_watchdog_not_a_stale_socket_lease():
    assert 30 <= CONTROL_HEARTBEAT_TIMEOUT_SECS <= 120


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
async def test_machine_control_result_reconcile_prefers_live_serializer(monkeypatch):
    calls = []

    class FakeLiveSerializer:
        is_configured = True

        async def execute(self, fn, *, auto_commit, label):
            calls.append(("live_execute", label, auto_commit))
            return fn("live-db")

    def fake_live_reconcile(db, message, *, owner_id, device_id):
        calls.append(("live_reconcile", db, message["command_id"], owner_id, device_id))
        return True

    def archive_reconcile_must_not_run(*_args, **_kwargs):
        raise AssertionError("live machine-control operations should reconcile before archive")

    monkeypatch.setattr("zerg.routers.agents_control.database_module.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.routers.agents_control.get_live_write_serializer", lambda: FakeLiveSerializer())
    monkeypatch.setattr(
        "zerg.routers.agents_control.reconcile_live_machine_control_operation_from_command_result",
        fake_live_reconcile,
    )
    monkeypatch.setattr(
        "zerg.routers.agents_control.reconcile_machine_control_operation_from_command_result",
        archive_reconcile_must_not_run,
    )

    matched = await _reconcile_machine_control_operation_result(
        "fallback-db",
        {"command_id": "machine-op:test"},
        owner_id=7,
        device_id="cinder",
    )

    assert matched is True
    assert calls == [
        ("live_execute", "live-machine-control-result", False),
        ("live_reconcile", "live-db", "machine-op:test", 7, "cinder"),
    ]


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
