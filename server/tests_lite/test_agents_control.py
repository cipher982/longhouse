import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers.agents_control import CONTROL_HEARTBEAT_TIMEOUT_SECS
from zerg.routers.agents_control import _reconcile_late_launch_result
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
async def test_late_launch_result_reconcile_uses_write_serializer(monkeypatch):
    calls = []

    class FakeSerializer:
        async def execute_or_direct(self, fn, fallback_db, *, auto_commit, label):
            calls.append(("execute", label, fallback_db, auto_commit))
            return fn("serializer-db")

    def fake_reconcile(db, message):
        calls.append(("reconcile", db, message["command_id"]))
        return False

    monkeypatch.setattr("zerg.routers.agents_control.get_write_serializer", lambda: FakeSerializer())
    monkeypatch.setattr("zerg.routers.agents_control.reconcile_launch_from_command_result", fake_reconcile)

    matched = await _reconcile_late_launch_result("fallback-db", {"command_id": "launch:test"})

    assert matched is False
    assert calls == [
        ("execute", "remote-launch-result", "fallback-db", False),
        ("reconcile", "serializer-db", "launch:test"),
    ]
