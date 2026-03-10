"""Tests for extracted single-commis continuation helper."""

from __future__ import annotations

import pytest

from zerg.models.enums import RunStatus
from zerg.models.models import Run
from zerg.services.commis_single_resume import continue_oikos


class _DummyQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result


class _DummyDb:
    def __init__(self, run_result):
        self._run_result = run_result

    def query(self, model):
        if model is Run:
            return _DummyQuery(self._run_result)
        return _DummyQuery(None)


@pytest.mark.asyncio
async def test_continue_oikos_returns_none_when_run_missing():
    db = _DummyDb(run_result=None)

    async def _unused_runner_factory(*_args, **_kwargs):
        raise AssertionError("runner_factory should not be called when run is missing")

    result = await continue_oikos(
        db=db,
        run_id=123,
        commis_result="ok",
        runner_factory=_unused_runner_factory,
    )

    assert result is None


@pytest.mark.asyncio
async def test_continue_oikos_skips_when_run_not_waiting():
    run = type("RunObj", (), {"status": RunStatus.SUCCESS})()
    db = _DummyDb(run_result=run)

    async def _unused_runner_factory(*_args, **_kwargs):
        raise AssertionError("runner_factory should not be called when run is not WAITING")

    result = await continue_oikos(
        db=db,
        run_id=456,
        commis_result="ok",
        runner_factory=_unused_runner_factory,
    )

    assert result == {"status": "skipped", "reason": "run is success, not waiting", "run_id": 456}
