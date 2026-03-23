from __future__ import annotations

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
