from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from zerg.services.runner_job_dispatcher import PendingJob
from zerg.services.runner_job_dispatcher import RunnerJobDispatcher


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


@pytest.mark.asyncio
async def test_dispatch_job_cancellation_clears_runner_and_pending(monkeypatch):
    dispatcher = RunnerJobDispatcher()
    runner_id = 7
    job_id = "job-cancel"
    cleanup_errors: list[tuple[str, str]] = []

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
        "zerg.services.runner_job_dispatcher.runner_crud.update_job_error",
        lambda _db, _job_id, error: cleanup_errors.append((_job_id, error)),
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

    assert cleanup_errors == [(job_id, "Dispatch cancelled")]
    assert dispatcher.can_accept_job(runner_id) is True
    with dispatcher._pending_lock:
        assert job_id not in dispatcher._pending_jobs
