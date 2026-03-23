"""Tests for commis inbox trigger helper."""

from __future__ import annotations

import pytest

from zerg.models.enums import RunStatus
from zerg.models.models import Run
from zerg.services.commis_inbox_trigger import trigger_commis_inbox_run


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
async def test_trigger_commis_inbox_run_skips_when_original_run_missing():
    db = _DummyDb(run_result=None)

    result = await trigger_commis_inbox_run(
        db=db,
        original_run_id=123,
        commis_job_id=1,
        commis_result="ok",
        commis_status="success",
    )

    assert result == {"status": "skipped", "reason": "original run not found"}


@pytest.mark.asyncio
async def test_trigger_commis_inbox_run_skips_when_original_not_terminal():
    run = type("RunObj", (), {"status": RunStatus.RUNNING})()
    db = _DummyDb(run_result=run)

    result = await trigger_commis_inbox_run(
        db=db,
        original_run_id=456,
        commis_job_id=2,
        commis_result="ok",
        commis_status="success",
    )

    assert result == {"status": "skipped", "reason": "original run is running"}
