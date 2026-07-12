from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from zerg.services import archive_api_reader_status as reader_status


@pytest.fixture(autouse=True)
def isolated_reader_status(tmp_path, monkeypatch):
    status_path = tmp_path / "archive-worker-status.json"
    monkeypatch.setattr(reader_status, "archive_worker_status_path", lambda: status_path)
    monkeypatch.setattr(reader_status, "_state_pid", os.getpid())
    monkeypatch.setattr(reader_status, "_active_count", 0)
    yield
    monkeypatch.setattr(reader_status, "_active_count", 0)


def test_reader_activity_is_refcounted_and_background_can_opt_out():
    path = reader_status.archive_api_reader_status_root() / f"{os.getpid()}.json"

    with reader_status.archive_api_reader_activity():
        assert reader_status.archive_api_reader_busy()
        assert json.loads(path.read_text())["active_count"] == 1
        with reader_status.archive_api_reader_activity():
            assert json.loads(path.read_text())["active_count"] == 2
        assert reader_status.archive_api_reader_busy()
        assert json.loads(path.read_text())["active_count"] == 1
        with reader_status.archive_api_reader_activity(enabled=False):
            assert json.loads(path.read_text())["active_count"] == 1

    assert not reader_status.archive_api_reader_busy()
    assert not path.exists()


@pytest.mark.asyncio
async def test_reader_activity_clears_when_request_task_is_cancelled():
    entered = asyncio.Event()

    async def request_scope() -> None:
        with reader_status.archive_api_reader_activity():
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(request_scope())
    await entered.wait()
    assert reader_status.archive_api_reader_busy()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not reader_status.archive_api_reader_busy()


def test_reader_busy_ignores_stale_and_dead_processes(monkeypatch):
    root = reader_status.archive_api_reader_status_root()
    root.mkdir(parents=True)
    stale = root / "111.json"
    dead = root / "222.json"
    stale.write_text(
        json.dumps({"pid": 111, "active_count": 1, "observed_at_unix": time.time() - 121}),
        encoding="utf-8",
    )
    dead.write_text(
        json.dumps({"pid": 222, "active_count": 1, "observed_at_unix": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(reader_status, "_pid_is_alive", lambda pid: pid == 111)

    assert not reader_status.archive_api_reader_busy()
    assert not stale.exists()
    assert not dead.exists()
