import pytest

from zerg.lifespan import _live_preview_cleanup_enabled
from zerg.lifespan import _reap_stale_machine_control_operations_once
from zerg.lifespan import _session_input_queue_recovery_enabled


def test_live_preview_cleanup_is_opt_in(monkeypatch):
    monkeypatch.delenv("LONGHOUSE_ENABLE_LIVE_PREVIEW_CLEANUP", raising=False)

    assert _live_preview_cleanup_enabled() is False


def test_live_preview_cleanup_can_be_enabled(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ENABLE_LIVE_PREVIEW_CLEANUP", "true")

    assert _live_preview_cleanup_enabled() is True


def test_session_input_queue_recovery_is_opt_in(monkeypatch):
    monkeypatch.delenv("LONGHOUSE_ENABLE_SESSION_INPUT_QUEUE_RECOVERY", raising=False)

    assert _session_input_queue_recovery_enabled() is False


def test_session_input_queue_recovery_can_be_enabled(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ENABLE_SESSION_INPUT_QUEUE_RECOVERY", "true")

    assert _session_input_queue_recovery_enabled() is True


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
