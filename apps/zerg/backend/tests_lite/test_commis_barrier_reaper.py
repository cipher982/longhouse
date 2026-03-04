"""Tests for commis barrier reaper helper."""

from __future__ import annotations

import pytest

from zerg.models.commis_barrier import CommisBarrier
from zerg.models.commis_barrier import CommisBarrierJob
from zerg.models.models import CommisJob
from zerg.services.commis_barrier_reaper import reap_expired_barriers


class _DummyQuery:
    def __init__(self, db, model):
        self._db = db
        self._model = model

    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self, *_args, **_kwargs):
        return self

    def all(self):
        queue = self._db._all_results.setdefault(self._model, [])
        if queue:
            return queue.pop(0)
        return []

    def first(self):
        queue = self._db._first_results.setdefault(self._model, [])
        if queue:
            return queue.pop(0)
        return None


class _DummyDb:
    def __init__(self, *, all_results=None, first_results=None):
        self._all_results = all_results or {}
        self._first_results = first_results or {}
        self.commit_count = 0
        self.rollback_count = 0

    def query(self, model):
        return _DummyQuery(self, model)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_reap_expired_barriers_returns_zero_when_none_found():
    db = _DummyDb(all_results={CommisBarrier: [[]]})

    async def _resume_batch(**_kwargs):
        raise AssertionError("resume_batch should not be called when no expired barriers")

    result = await reap_expired_barriers(db, resume_batch=_resume_batch)

    assert result == {"reaped": 0}
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_reap_expired_barriers_reaps_and_cleans_orphans():
    barrier = type("Barrier", (), {"id": 1, "run_id": 44, "status": "waiting"})()
    timeout_job = type(
        "BarrierJob",
        (),
        {"tool_call_id": "tc-timeout", "result": None, "error": None, "status": "created", "completed_at": None},
    )()
    done_job = type(
        "BarrierJob",
        (),
        {"tool_call_id": "tc-done", "result": "ok", "error": None, "status": "completed", "completed_at": None},
    )()
    orphan_job = type(
        "CommisJobObj",
        (),
        {"id": 99, "status": "created", "error": None, "finished_at": None},
    )()

    db = _DummyDb(
        all_results={
            CommisBarrier: [[barrier]],
            CommisBarrierJob: [[timeout_job], [timeout_job, done_job]],
            CommisJob: [[orphan_job]],
        },
        first_results={
            CommisBarrier: [barrier],
            CommisBarrierJob: [None],  # orphan check: no barrier job for orphan
        },
    )

    resume_calls = []

    async def _resume_batch(**kwargs):
        resume_calls.append(kwargs)
        return {"status": "success"}

    result = await reap_expired_barriers(db, resume_batch=_resume_batch)

    assert result["reaped"] == 1
    assert result["orphans_cleaned"] == 1
    assert result["details"][0]["barrier_id"] == 1
    assert result["details"][0]["run_id"] == 44
    assert result["details"][0]["timeout_count"] == 1
    assert result["details"][0]["result"] == "success"

    assert timeout_job.status == "timeout"
    assert timeout_job.error == "Commis timed out (deadline exceeded)"

    assert orphan_job.status == "failed"
    assert orphan_job.error == "Orphaned job - barrier creation failed"
    assert orphan_job.finished_at is not None

    assert len(resume_calls) == 1
    assert resume_calls[0]["run_id"] == 44
    assert resume_calls[0]["commis_results"][0]["status"] == "timeout"
    assert db.commit_count >= 2
