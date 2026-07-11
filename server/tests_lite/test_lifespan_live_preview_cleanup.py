import pytest

from zerg.lifespan import _reap_stale_machine_control_operations_once


@pytest.mark.asyncio
async def test_machine_control_reaper_uses_write_serializer(monkeypatch):
    calls = []

    def fake_reap(db):
        calls.append(("reap", db))
        return 3

    class FakeSerializer:
        async def execute(self, fn, *, auto_commit, label):
            calls.append(("execute", label, auto_commit))
            return fn("serializer-db")

    monkeypatch.setattr(
        "zerg.services.machine_control_operations.reap_stale_machine_control_operations",
        fake_reap,
    )
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: FakeSerializer(),
    )

    assert await _reap_stale_machine_control_operations_once() == 3
    assert calls == [
        ("execute", "machine-control-reaper", False),
        ("reap", "serializer-db"),
    ]
