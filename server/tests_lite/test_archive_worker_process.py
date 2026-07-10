from __future__ import annotations

import os
import asyncio
import json
import subprocess
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentHeartbeat
from zerg.models.live_store import LiveArchiveOutbox
from zerg.services.archive_worker_status import archive_worker_enabled
from zerg.services.archive_worker_status import read_archive_worker_status
from zerg.services.archive_worker_status import write_archive_worker_status
from zerg.services.live_archive_outbox import enqueue_heartbeat_stamp_outbox
from zerg.utils.time import normalize_utc


def _worker_env(tmp_path, archive_url: str, live_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": archive_url,
        "LONGHOUSE_LIVE_DATABASE_URL": live_url,
        "LONGHOUSE_ARCHIVE_WORKER_ENABLED": "1",
        "LONGHOUSE_ARCHIVE_WORKER_STATUS_PATH": str(tmp_path / "archive-worker-status.json"),
        "TESTING": "0",
    }


def _seed_worker_databases(tmp_path):
    archive_url = f"sqlite:///{tmp_path / 'archive.db'}"
    live_url = f"sqlite:///{tmp_path / 'live.db'}"
    archive_engine = make_engine(archive_url)
    live_engine = make_engine(live_url)
    Base.metadata.create_all(archive_engine)
    initialize_live_database(live_engine)
    archive_factory = make_sessionmaker(archive_engine)
    live_factory = make_sessionmaker(live_engine)

    received_at = datetime.now(timezone.utc)
    with live_factory() as db:
        assert enqueue_heartbeat_stamp_outbox(
            db,
            {
                "device_id": "worker-test-device",
                "received_at": received_at,
                "version": "test",
                "disk_free_bytes": 1234,
            },
        )
        db.commit()
    return archive_url, live_url, archive_factory, live_factory, received_at


def _run_worker(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "zerg.services.archive_worker", "--once"],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_archive_worker_drains_real_heartbeat_outbox_in_child_process(tmp_path, monkeypatch):
    archive_url, live_url, archive_factory, live_factory, received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url, live_url)

    result = _run_worker(env)

    assert result.returncode == 0, result.stderr
    with archive_factory() as db:
        row = db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "worker-test-device").one()
        assert normalize_utc(row.received_at) == received_at
    with live_factory() as db:
        outbox = db.query(LiveArchiveOutbox).one()
        assert outbox.drained_at is not None
        assert outbox.attempts == 1

    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_STATUS_PATH", env["LONGHOUSE_ARCHIVE_WORKER_STATUS_PATH"])
    status = read_archive_worker_status()
    assert status["status"] == "stopped"
    assert status["drained"] == 1


def test_archive_worker_native_style_exit_leaves_outbox_pending_and_parent_alive(tmp_path):
    archive_url, live_url, archive_factory, live_factory, _received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url, live_url)
    env["LONGHOUSE_ARCHIVE_WORKER_TEST_EXIT_BEFORE_DRAIN"] = "1"
    parent_pid = os.getpid()

    result = _run_worker(env)

    assert result.returncode == 91
    assert os.getpid() == parent_pid
    with archive_factory() as db:
        assert db.query(AgentHeartbeat).count() == 0
    with live_factory() as db:
        outbox = db.query(LiveArchiveOutbox).one()
        assert outbox.drained_at is None
        assert outbox.attempts == 0
        assert enqueue_heartbeat_stamp_outbox(
            db,
            {
                "device_id": "hot-write-during-cold-exit",
                "received_at": datetime.now(timezone.utc),
                "version": "test",
                "disk_free_bytes": 4321,
            },
        )
        db.commit()


def test_archive_worker_defaults_off_in_tests_and_can_be_enabled(monkeypatch):
    monkeypatch.delenv("LONGHOUSE_ARCHIVE_WORKER_ENABLED", raising=False)
    monkeypatch.setenv("TESTING", "1")
    assert archive_worker_enabled() is False

    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_ENABLED", "1")
    assert archive_worker_enabled() is True


def test_archive_worker_stale_running_status_is_degraded(tmp_path, monkeypatch):
    path = tmp_path / "archive-worker-status.json"
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_STATUS_PATH", str(path))
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_STATUS_STALE_SECONDS", "2")
    write_archive_worker_status({"status": "running", "pid": 123})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = read_archive_worker_status()

    assert status["status"] == "degraded"
    assert status["reason"] == "status_stale"
    assert status["age_seconds"] >= 59


@pytest.mark.asyncio
async def test_supervisor_restarts_killed_worker_and_drains_next_row(tmp_path, monkeypatch):
    archive_url, live_url, archive_factory, live_factory, _received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url, live_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_INTERVAL_SECONDS", "0.05")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_WORKER_MAX_BACKOFF_SECONDS", "1")

    import zerg.services.archive_worker_supervisor as supervisor

    async def wait_until(predicate, timeout: float = 5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("condition was not reached before timeout")

    def drained_count() -> int:
        with live_factory() as db:
            return db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.drained_at.isnot(None)).count()

    supervisor.start_archive_worker_supervisor()
    try:
        await wait_until(lambda: drained_count() == 1)
        first_process = supervisor._worker_process
        assert first_process is not None
        first_pid = first_process.pid
        first_process.kill()

        await wait_until(
            lambda: supervisor._worker_process is not None
            and supervisor._worker_process.pid != first_pid
            and supervisor._worker_process.returncode is None
        )

        with live_factory() as db:
            assert enqueue_heartbeat_stamp_outbox(
                db,
                {
                    "device_id": "worker-test-device-2",
                    "received_at": datetime.now(timezone.utc),
                    "version": "test",
                    "disk_free_bytes": 5678,
                },
            )
            db.commit()
        await wait_until(lambda: drained_count() == 2)
        with archive_factory() as db:
            assert db.query(AgentHeartbeat).count() == 2
    finally:
        await supervisor.stop_archive_worker_supervisor()
