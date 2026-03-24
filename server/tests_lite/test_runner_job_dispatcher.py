from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from zerg.services.runner_job_dispatcher import PendingJob
from zerg.services.runner_job_dispatcher import RunnerJobDispatcher
from zerg.utils.time import utc_now_naive


class _SpyEvent:
    def __init__(self, on_set):
        self._on_set = on_set
        self.set_calls = 0

    def set(self) -> None:
        self.set_calls += 1
        self._on_set()


def test_complete_job_clears_runner_before_signaling_waiter():
    dispatcher = RunnerJobDispatcher()
    runner_id = 7
    job_id = "job-1"
    observed_can_accept: list[bool] = []

    dispatcher.mark_job_active(runner_id, job_id)
    pending = PendingJob(event=_SpyEvent(lambda: observed_can_accept.append(dispatcher.can_accept_job(runner_id))))

    with dispatcher._pending_lock:
        dispatcher._pending_jobs[job_id] = pending

    result = {"ok": True, "data": {"exit_code": 0}}
    dispatcher.complete_job(job_id, result, runner_id=runner_id)

    assert pending.result == result
    assert pending.event.set_calls == 1
    assert observed_can_accept == [True]
    assert dispatcher.can_accept_job(runner_id) is True


def test_complete_job_does_not_clear_newer_active_job():
    dispatcher = RunnerJobDispatcher()
    runner_id = 7
    dispatcher.mark_job_active(runner_id, "job-new")

    dispatcher.complete_job("job-old", {"ok": True}, runner_id=runner_id)

    assert dispatcher.get_active_job_id(runner_id) == "job-new"
    assert dispatcher.can_accept_job(runner_id) is False


@pytest.mark.asyncio
async def test_dispatch_job_cancellation_clears_runner_and_pending(monkeypatch):
    dispatcher = RunnerJobDispatcher()
    runner_id = 7
    job_id = "job-cancel"
    timed_out_jobs: list[str] = []

    class _FakeConnectionManager:
        def is_online(self, owner_id: int, runner_id: int) -> bool:
            return True

        async def send_to_runner(self, *, owner_id: int, runner_id: int, message: dict) -> bool:
            return True

    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.get_runner_connection_manager",
        lambda: _FakeConnectionManager(),
    )
    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.runner_crud.create_runner_job",
        lambda **kwargs: SimpleNamespace(id=job_id),
    )
    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.runner_crud.update_job_started",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.runner_crud.update_job_timeout",
        lambda _db, _job_id: timed_out_jobs.append(_job_id),
    )

    task = asyncio.create_task(
        dispatcher.dispatch_job(
            db=object(),
            owner_id=1,
            runner_id=runner_id,
            command="echo hi",
            timeout_secs=60,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert timed_out_jobs == [job_id]
    assert dispatcher.can_accept_job(runner_id) is True
    with dispatcher._pending_lock:
        assert job_id not in dispatcher._pending_jobs


@pytest.mark.asyncio
async def test_dispatch_job_reclaims_stale_active_job(monkeypatch):
    dispatcher = RunnerJobDispatcher()
    runner_id = 7
    fake_jobs: dict[str, SimpleNamespace] = {}
    stale_job = SimpleNamespace(
        id="job-stale",
        status="running",
        started_at=utc_now_naive() - timedelta(seconds=30),
        created_at=utc_now_naive() - timedelta(seconds=31),
        timeout_secs=5,
    )
    fake_jobs[stale_job.id] = stale_job
    next_job_id = 0

    def _create_runner_job(**kwargs):
        nonlocal next_job_id
        next_job_id += 1
        job = SimpleNamespace(
            id=f"job-new-{next_job_id}",
            status="queued",
            started_at=None,
            created_at=utc_now_naive(),
            timeout_secs=kwargs["timeout_secs"],
        )
        fake_jobs[job.id] = job
        return job

    def _update_job_started(_db, job_id: str):
        job = fake_jobs[job_id]
        job.status = "running"
        job.started_at = utc_now_naive()
        return job

    def _update_job_timeout(_db, job_id: str):
        job = fake_jobs[job_id]
        job.status = "timeout"
        return job

    class _FakeConnectionManager:
        def is_online(self, owner_id: int, runner_id: int) -> bool:
            return True

        async def send_to_runner(self, *, owner_id: int, runner_id: int, message: dict) -> bool:
            dispatcher.complete_job(
                message["job_id"],
                {
                    "ok": True,
                    "data": {
                        "job_id": message["job_id"],
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "duration_ms": 0,
                    },
                },
                runner_id=runner_id,
            )
            return True

    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.get_runner_connection_manager",
        lambda: _FakeConnectionManager(),
    )
    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.runner_crud.get_job",
        lambda _db, job_id: fake_jobs.get(job_id),
    )
    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.runner_crud.create_runner_job",
        _create_runner_job,
    )
    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.runner_crud.update_job_started",
        _update_job_started,
    )
    monkeypatch.setattr(
        "zerg.services.runner_job_dispatcher.runner_crud.update_job_timeout",
        _update_job_timeout,
    )

    dispatcher.mark_job_active(runner_id, stale_job.id)
    with dispatcher._pending_lock:
        dispatcher._pending_jobs[stale_job.id] = PendingJob(event=threading.Event())

    result = await dispatcher.dispatch_job(
        db=object(),
        owner_id=1,
        runner_id=runner_id,
        command="echo recovered",
        timeout_secs=5,
    )

    assert result["ok"] is True
    assert fake_jobs[stale_job.id].status == "timeout"
    assert dispatcher.can_accept_job(runner_id) is True
    with dispatcher._pending_lock:
        assert stale_job.id not in dispatcher._pending_jobs
