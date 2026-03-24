from __future__ import annotations

import logging

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.models import Runner
from zerg.models.models import RunnerJob
from zerg.models.user import User
from zerg.routers import runners as runners_router
from zerg.services.runner_job_dispatcher import PendingJob
from zerg.services.runner_job_dispatcher import RunnerJobDispatcher
from zerg.utils.time import utc_now_naive


def _make_db(tmp_path):
    db_path = tmp_path / "runner-websocket-job-persistence.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


class _SpyEvent:
    def __init__(self):
        self.set_calls = 0

    def set(self) -> None:
        self.set_calls += 1


class _FakeSerializer:
    def __init__(self, SessionLocal, *, error: Exception | None = None):
        self.SessionLocal = SessionLocal
        self.error = error
        self.calls: list[tuple[str, bool]] = []
        self.is_configured = True

    async def execute(self, fn, *, label: str = "", auto_commit: bool = True):
        self.calls.append((label, auto_commit))
        if self.error is not None:
            raise self.error
        with self.SessionLocal() as db:
            return fn(db)


def _seed_runner_job(SessionLocal):
    with SessionLocal() as db:
        user = User(email="runner-job@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        runner = Runner(
            owner_id=user.id,
            name="cinder",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            last_seen_at=utc_now_naive(),
            runner_metadata={"capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        job = RunnerJob(
            id="job-1",
            owner_id=user.id,
            runner_id=runner.id,
            command="echo hi",
            timeout_secs=30,
            status="running",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return user.id, runner.id, job.id


def _mark_dispatcher_pending(dispatcher: RunnerJobDispatcher, *, runner_id: int, job_id: str) -> PendingJob:
    dispatcher.mark_job_active(runner_id, job_id)
    pending = PendingJob(event=_SpyEvent())
    with dispatcher._pending_lock:
        dispatcher._pending_jobs[job_id] = pending
    return pending


@pytest.mark.asyncio
async def test_handle_exec_done_uses_write_serializer_and_clears_dispatcher(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    owner_id, runner_id, job_id = _seed_runner_job(SessionLocal)
    dispatcher = RunnerJobDispatcher()
    pending = _mark_dispatcher_pending(dispatcher, runner_id=runner_id, job_id=job_id)
    serializer = _FakeSerializer(SessionLocal)
    monkeypatch.setattr(runners_router, "get_write_serializer", lambda: serializer)

    with SessionLocal() as db:
        await runners_router._handle_exec_done(
            db,
            {"job_id": job_id, "exit_code": 0, "duration_ms": 123},
            runner_id,
            owner_id,
            dispatcher,
        )

    with SessionLocal() as db:
        job = db.query(RunnerJob).filter(RunnerJob.id == job_id).one()
        assert job.status == "success"
        assert job.exit_code == 0
        assert job.finished_at is not None

    assert serializer.calls == [("runner-job-complete", False)]
    assert dispatcher.can_accept_job(runner_id) is True
    assert pending.event.set_calls == 1
    assert pending.result is not None
    assert pending.result["ok"] is True
    assert pending.result["data"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_handle_exec_done_write_failure_returns_error_and_frees_runner(tmp_path, monkeypatch, caplog):
    SessionLocal = _make_db(tmp_path)
    owner_id, runner_id, job_id = _seed_runner_job(SessionLocal)
    dispatcher = RunnerJobDispatcher()
    pending = _mark_dispatcher_pending(dispatcher, runner_id=runner_id, job_id=job_id)
    serializer = _FakeSerializer(SessionLocal, error=RuntimeError("database is locked"))
    monkeypatch.setattr(runners_router, "get_write_serializer", lambda: serializer)
    caplog.set_level(logging.ERROR, logger="zerg.routers.runners")

    with SessionLocal() as db:
        await runners_router._handle_exec_done(
            db,
            {"job_id": job_id, "exit_code": 0, "duration_ms": 123},
            runner_id,
            owner_id,
            dispatcher,
        )

    with SessionLocal() as db:
        job = db.query(RunnerJob).filter(RunnerJob.id == job_id).one()
        assert job.status == "running"
        assert job.finished_at is None

    assert "Failed to persist exec_done" in caplog.text
    assert serializer.calls == [("runner-job-complete", False)]
    assert dispatcher.can_accept_job(runner_id) is True
    assert pending.event.set_calls == 1
    assert pending.result is not None
    assert pending.result["ok"] is False
    assert "Failed to persist runner completion" in pending.result["error"]["message"]


@pytest.mark.asyncio
async def test_handle_exec_chunk_uses_write_serializer(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    owner_id, runner_id, job_id = _seed_runner_job(SessionLocal)
    serializer = _FakeSerializer(SessionLocal)
    monkeypatch.setattr(runners_router, "get_write_serializer", lambda: serializer)

    with SessionLocal() as db:
        await runners_router._handle_exec_chunk(
            db,
            {"job_id": job_id, "stream": "stdout", "data": "hello from chunk"},
            runner_id,
            owner_id,
        )

    with SessionLocal() as db:
        job = db.query(RunnerJob).filter(RunnerJob.id == job_id).one()
        assert "hello from chunk" in (job.stdout_trunc or "")

    assert serializer.calls == [("runner-output", False)]
