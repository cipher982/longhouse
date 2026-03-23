"""Tests for commis inbox follow-up helpers."""

from __future__ import annotations

import contextvars

import pytest

from zerg.models.enums import RunStatus
from zerg.services.commis_inbox_followup import INBOX_QUEUED_RESULT
from zerg.services.commis_inbox_followup import run_inbox_followup_after_run
from zerg.services.commis_inbox_followup import schedule_inbox_followup_after_run


class _DummyQuery:
    def __init__(self, run):
        self._run = run

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._run


class _DummyDb:
    def __init__(self, run):
        self._run = run
        self.closed = False
        self.expired = 0

    def query(self, _model):
        return _DummyQuery(self._run)

    def expire_all(self):
        self.expired += 1

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_run_inbox_followup_after_run_triggers_continuation(monkeypatch):
    run = type("RunObj", (), {"status": RunStatus.SUCCESS})()
    db = _DummyDb(run)
    monkeypatch.setattr("zerg.database.get_session_factory", lambda: (lambda: db))

    calls = []

    async def _fake_trigger_followup(**kwargs):
        calls.append(kwargs)
        return {"status": "ok"}

    result = await run_inbox_followup_after_run(
        run_id=123,
        commis_job_id=7,
        commis_status="success",
        commis_error=None,
        trigger_followup=_fake_trigger_followup,
    )

    assert result == {"status": "ok"}
    assert db.closed is True
    assert len(calls) == 1
    assert calls[0]["db"] is db
    assert calls[0]["original_run_id"] == 123
    assert calls[0]["commis_job_id"] == 7
    assert calls[0]["commis_result"] == INBOX_QUEUED_RESULT


@pytest.mark.asyncio
async def test_run_inbox_followup_after_run_returns_none_when_run_missing(monkeypatch):
    db = _DummyDb(run=None)
    monkeypatch.setattr("zerg.database.get_session_factory", lambda: (lambda: db))

    async def _fake_trigger_followup(**_kwargs):
        raise AssertionError("trigger_followup should not be called when run is missing")

    result = await run_inbox_followup_after_run(
        run_id=321,
        commis_job_id=8,
        commis_status="failed",
        commis_error="boom",
        trigger_followup=_fake_trigger_followup,
    )

    assert result is None
    assert db.closed is True


def test_schedule_inbox_followup_after_run_schedules_task_with_context(monkeypatch):
    captured = {}

    async def _fake_run_inbox_followup_after_run(**kwargs):
        captured["kwargs"] = kwargs
        return None

    def _fake_create_task(coro, context=None):
        captured["coro"] = coro
        captured["context"] = context
        coro.close()
        return object()

    monkeypatch.setattr(
        "zerg.services.commis_inbox_followup.run_inbox_followup_after_run",
        _fake_run_inbox_followup_after_run,
    )
    monkeypatch.setattr("zerg.services.commis_inbox_followup.asyncio.create_task", _fake_create_task)

    async def _fake_trigger_followup(**_kwargs):
        return None

    schedule_inbox_followup_after_run(
        run_id=999,
        commis_job_id=42,
        commis_status="success",
        commis_error=None,
        trigger_followup=_fake_trigger_followup,
    )

    assert captured["coro"] is not None
    assert isinstance(captured["context"], contextvars.Context)
