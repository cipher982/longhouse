"""Tests for commis barrier coordination helper."""

from __future__ import annotations

import pytest

from zerg.models.commis_barrier import CommisBarrier
from zerg.models.commis_barrier import CommisBarrierJob
from zerg.services.commis_barrier import check_and_resume_if_all_complete


class _DummyTx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummyQuery:
    def __init__(self, db, model):
        self._db = db
        self._model = model

    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self, *_args, **_kwargs):
        return self

    def first(self):
        if self._model is CommisBarrier:
            return self._db.barrier
        if self._model is CommisBarrierJob:
            return self._db.barrier_job
        return None

    def all(self):
        if self._model is CommisBarrierJob:
            return self._db.all_jobs
        return []


class _DummyDb:
    def __init__(self, *, barrier, barrier_job, all_jobs):
        self.barrier = barrier
        self.barrier_job = barrier_job
        self.all_jobs = all_jobs
        self.flush_count = 0

    def begin_nested(self):
        return _DummyTx()

    def query(self, model):
        return _DummyQuery(self, model)

    def flush(self):
        self.flush_count += 1


@pytest.mark.asyncio
async def test_check_and_resume_skips_when_no_barrier():
    db = _DummyDb(barrier=None, barrier_job=None, all_jobs=[])

    result = await check_and_resume_if_all_complete(
        db=db,
        run_id=1,
        job_id=10,
        result="ok",
    )

    assert result == {"status": "skipped", "reason": "no barrier found"}


@pytest.mark.asyncio
async def test_check_and_resume_returns_waiting_when_barrier_incomplete():
    barrier = type("Barrier", (), {"id": 11, "status": "waiting", "completed_count": 0, "expected_count": 2})()
    barrier_job = type(
        "BarrierJob",
        (),
        {"id": 100, "job_id": 55, "tool_call_id": "tc-55", "status": "created", "result": None, "error": None, "completed_at": None},
    )()
    db = _DummyDb(barrier=barrier, barrier_job=barrier_job, all_jobs=[barrier_job])

    result = await check_and_resume_if_all_complete(
        db=db,
        run_id=2,
        job_id=55,
        result="partial",
    )

    assert result == {"status": "waiting", "completed": 1, "expected": 2}
    assert barrier.completed_count == 1
    assert barrier.status == "waiting"
    assert barrier_job.status == "completed"
    assert db.flush_count == 0


@pytest.mark.asyncio
async def test_check_and_resume_returns_resume_when_barrier_complete():
    barrier = type("Barrier", (), {"id": 21, "status": "waiting", "completed_count": 0, "expected_count": 1})()
    barrier_job = type(
        "BarrierJob",
        (),
        {"id": 200, "job_id": 77, "tool_call_id": "tc-77", "status": "created", "result": None, "error": None, "completed_at": None},
    )()
    db = _DummyDb(barrier=barrier, barrier_job=barrier_job, all_jobs=[barrier_job])

    result = await check_and_resume_if_all_complete(
        db=db,
        run_id=3,
        job_id=77,
        result="done",
        error=None,
    )

    assert result["status"] == "resume"
    assert result["commis_results"] == [
        {
            "tool_call_id": "tc-77",
            "result": "done",
            "error": None,
            "status": "completed",
            "job_id": 77,
        }
    ]
    assert barrier.status == "resuming"
    assert barrier.completed_count == 1
    assert db.flush_count == 1
