from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from cryptography.fernet import Fernet

from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.live_store import LiveArchiveOutbox
from zerg.services.archive_worker_status import archive_worker_enabled
from zerg.services.archive_worker_status import read_archive_worker_status
from zerg.services.archive_worker_status import write_archive_worker_status
from zerg.services.live_archive_outbox import enqueue_heartbeat_stamp_outbox
from zerg.utils.time import normalize_utc


def _worker_env(tmp_path, archive_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": archive_url,
        "AUTH_DISABLED": "1",
        "FERNET_SECRET": Fernet.generate_key().decode(),
        "TESTING": "0",
    }


def _seed_worker_databases(tmp_path):
    archive_url = f"sqlite:///{tmp_path / 'archive.db'}"
    live_url = f"sqlite:///{tmp_path / 'archive-live.db'}"
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


def _ingest_job_payload() -> dict:
    session_id = "12345678-1234-5678-1234-567812345678"
    return {
        "data": {
            "id": session_id,
            "provider": "codex",
            "environment": "test",
            "project": "archive-worker",
            "device_id": "worker-test-device",
            "cwd": "/tmp",
            "started_at": "2026-07-09T12:00:00Z",
            "events": [
                {
                    "role": "user",
                    "content_text": "durable worker ingest",
                    "timestamp": "2026-07-09T12:00:01Z",
                    "source_path": "/tmp/worker-session.jsonl",
                    "source_offset": 0,
                }
            ],
        },
        "write_label": "ingest-live",
        "batch_index": 0,
    }


def test_archive_worker_drains_real_heartbeat_outbox_in_child_process(tmp_path, monkeypatch):
    archive_url, live_url, archive_factory, live_factory, received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url)

    result = _run_worker(env)

    assert result.returncode == 0, result.stderr
    with archive_factory() as db:
        row = db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "worker-test-device").one()
        assert normalize_utc(row.received_at) == received_at
    with live_factory() as db:
        outbox = db.query(LiveArchiveOutbox).one()
        assert outbox.drained_at is not None
        assert outbox.attempts == 1

    monkeypatch.setenv("TESTING", "0")
    monkeypatch.setenv("DATABASE_URL", archive_url)
    status = read_archive_worker_status()
    assert status["status"] == "stopped"
    assert status["drained"] == 1


def test_archive_worker_native_style_exit_leaves_outbox_pending_and_parent_alive(tmp_path):
    archive_url, live_url, archive_factory, live_factory, _received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url)
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


def test_archive_worker_defers_while_api_writer_is_active(tmp_path, monkeypatch):
    archive_url, live_url, archive_factory, live_factory, _received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from zerg.services.archive_api_writer_status import write_archive_api_writer_status

    write_archive_api_writer_status({"active": True, "label": "ingest-live", "job_id": 1})
    deferred = _run_worker(env)

    assert deferred.returncode == 0, deferred.stderr
    with archive_factory() as db:
        assert db.query(AgentHeartbeat).count() == 0
    with live_factory() as db:
        assert db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.drained_at.is_(None)).count() == 1

    write_archive_api_writer_status({"active": False, "label": None, "job_id": None})
    drained = _run_worker(env)
    assert drained.returncode == 0, drained.stderr
    with archive_factory() as db:
        assert db.query(AgentHeartbeat).count() == 1


@pytest.mark.asyncio
async def test_archive_worker_process_executes_durable_ingest_job(tmp_path, monkeypatch):
    archive_url, live_url, archive_factory, _live_factory, _received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from zerg.services.archive_worker_jobs import submit_archive_worker_job

    submitted = asyncio.create_task(
        submit_archive_worker_job("session_ingest.v1", _ingest_job_payload(), timeout_seconds=10)
    )
    await asyncio.sleep(0.1)
    worker_result = await asyncio.to_thread(_run_worker, env)
    result = await submitted

    assert worker_result.returncode == 0, worker_result.stderr
    assert result["result"]["events_inserted"] == 1
    with archive_factory() as db:
        assert db.query(AgentSession).count() == 1
        assert db.query(AgentEvent).count() == 1


@pytest.mark.asyncio
async def test_archive_worker_recovers_ingest_job_after_native_exit(tmp_path, monkeypatch):
    archive_url, live_url, archive_factory, _live_factory, _received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from zerg.services.archive_worker_jobs import submit_archive_worker_job

    submitted = asyncio.create_task(
        submit_archive_worker_job("session_ingest.v1", _ingest_job_payload(), timeout_seconds=10)
    )
    await asyncio.sleep(0.1)
    crash_env = {**env, "LONGHOUSE_ARCHIVE_WORKER_TEST_EXIT_BEFORE_JOB": "1"}
    crashed = await asyncio.to_thread(_run_worker, crash_env)
    assert crashed.returncode == 92
    recovered = await asyncio.to_thread(_run_worker, env)
    result = await submitted

    assert recovered.returncode == 0, recovered.stderr
    assert result["result"]["events_inserted"] == 1
    with archive_factory() as db:
        assert db.query(AgentEvent).count() == 1


def test_archive_worker_is_canonical_outside_tests(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    assert archive_worker_enabled() is False

    monkeypatch.setenv("TESTING", "0")
    assert archive_worker_enabled() is True


def test_archive_ingest_worker_defaults_to_catalog_single_writer_mode(tmp_path, monkeypatch):
    import zerg.database as database_module
    from zerg.services.archive_ingest_job import archive_ingest_worker_enabled

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: False)
    assert archive_ingest_worker_enabled() is False

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    assert archive_ingest_worker_enabled() is True


def test_archive_worker_stale_running_status_is_degraded(tmp_path, monkeypatch):
    path = tmp_path / "archive-worker-status.json"
    monkeypatch.setenv("TESTING", "0")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    write_archive_worker_status({"status": "running", "pid": 123})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = read_archive_worker_status()

    assert status["status"] == "degraded"
    assert status["reason"] == "status_stale"
    assert status["age_seconds"] >= 59


def test_archive_worker_long_active_operation_is_degraded(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTING", "0")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    write_archive_worker_status(
        {
            "status": "running",
            "pid": 123,
            "active_operation": "job",
            "active_started_at_unix": (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp(),
        }
    )

    status = read_archive_worker_status()

    assert status["status"] == "degraded"
    assert status["reason"] == "operation_stalled"
    assert status["active_age_seconds"] >= 59


def test_archive_worker_scheduler_alternates_jobs_and_outbox():
    from zerg.services.archive_worker import _select_work_once

    selected: list[str] = []
    prefer_jobs = True
    for _ in range(6):
        job_processed, outbox_result, prefer_jobs = _select_work_once(
            prefer_jobs=prefer_jobs,
            process_job=lambda: selected.append("job") or True,
            drain_outbox=lambda: selected.append("outbox") or {"processed": 1, "drained": 1, "failed": 0},
        )
        assert job_processed or outbox_result["processed"]

    assert selected == ["job", "outbox", "job", "outbox", "job", "outbox"]


@pytest.mark.asyncio
async def test_supervisor_restarts_killed_worker_and_drains_next_row(tmp_path, monkeypatch):
    archive_url, live_url, archive_factory, live_factory, _received_at = _seed_worker_databases(tmp_path)
    env = _worker_env(tmp_path, archive_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
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

        # The parent remains alive and the hot database remains writable while
        # the cold worker is dead. Hot readiness reports degradation, not 503.
        with live_factory() as db:
            assert enqueue_heartbeat_stamp_outbox(
                db,
                {
                    "device_id": "hot-write-while-worker-dead",
                    "received_at": datetime.now(timezone.utc),
                    "version": "test",
                    "disk_free_bytes": 5678,
                },
            )
            db.commit()

        import zerg.database as database_module
        import zerg.routers.health as health_router

        monkeypatch.setattr(database_module, "get_live_engine", lambda: live_factory.kw["bind"])
        monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
        monkeypatch.setattr(health_router, "_write_serializer_stall_check", lambda: (False, {"status": "pass"}))
        monkeypatch.setattr(health_router, "_live_write_serializer_check", lambda: (False, {"status": "pass"}))
        monkeypatch.setattr(
            health_router,
            "_archive_worker_check",
            lambda: {"enabled": True, "status": "degraded", "reason": "worker_killed"},
        )
        readiness = health_router.readyz_check()
        assert readiness["status"] == "ready_with_archive_degraded"
        assert readiness["reason"] == "archive_worker_degraded"

        await wait_until(
            lambda: supervisor._worker_process is not None
            and supervisor._worker_process.pid != first_pid
            and supervisor._worker_process.returncode is None
        )
        await wait_until(lambda: int(read_archive_worker_status().get("restart_count") or 0) >= 1)
        assert read_archive_worker_status()["restart_backoff_seconds"] >= 1.0

        await wait_until(lambda: drained_count() == 2)
        with archive_factory() as db:
            assert db.query(AgentHeartbeat).count() == 2
    finally:
        await supervisor.stop_archive_worker_supervisor()
