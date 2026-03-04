"""Tests for shared Oikos run lifecycle helpers."""

from types import SimpleNamespace

import pytest

from zerg.services.oikos_run_lifecycle import emit_cancelled_run_updated
from zerg.services.oikos_run_lifecycle import emit_error_event_and_close_stream
from zerg.services.oikos_run_lifecycle import emit_failed_run_updated
from zerg.services.oikos_run_lifecycle import emit_oikos_waiting_and_run_updated
from zerg.services.oikos_run_lifecycle import emit_success_run_updated
from zerg.services.oikos_run_lifecycle import emit_stream_control_for_pending_commiss


class _DummyQuery:
    def __init__(self, pending_count: int):
        self._pending_count = pending_count

    def filter(self, *_args, **_kwargs):
        return self

    def count(self) -> int:
        return self._pending_count


class _DummyDb:
    def __init__(self, pending_count: int):
        self._pending_count = pending_count

    def query(self, _model):
        return _DummyQuery(self._pending_count)


@pytest.mark.asyncio
async def test_emit_oikos_waiting_and_run_updated_with_single_job(monkeypatch):
    calls = []

    async def _fake_emit_run_event(*, db, run_id, event_type, payload):
        calls.append(
            {
                "db": db,
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _fake_emit_run_event)

    db = object()
    await emit_oikos_waiting_and_run_updated(
        db=db,
        run_id=5,
        fiche_id=8,
        thread_id=13,
        owner_id=21,
        message_id="msg-1",
        message="Working in background",
        trace_id="trace-1",
        job_id=34,
    )

    assert calls == [
        {
            "db": db,
            "run_id": 5,
            "event_type": "oikos_waiting",
            "payload": {
                "fiche_id": 8,
                "thread_id": 13,
                "message": "Working in background",
                "owner_id": 21,
                "message_id": "msg-1",
                "close_stream": False,
                "trace_id": "trace-1",
                "job_id": 34,
            },
        },
        {
            "db": db,
            "run_id": 5,
            "event_type": "run_updated",
            "payload": {
                "fiche_id": 8,
                "status": "waiting",
                "thread_id": 13,
                "owner_id": 21,
            },
        },
    ]


@pytest.mark.asyncio
async def test_emit_oikos_waiting_and_run_updated_with_job_ids(monkeypatch):
    calls = []

    async def _fake_emit_run_event(*, db, run_id, event_type, payload):
        calls.append(
            {
                "db": db,
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _fake_emit_run_event)

    db = object()
    await emit_oikos_waiting_and_run_updated(
        db=db,
        run_id=6,
        fiche_id=9,
        thread_id=14,
        owner_id=22,
        message_id="msg-2",
        message="Working on many tasks",
        job_ids=[101, 102],
    )

    assert calls[0]["event_type"] == "oikos_waiting"
    assert calls[0]["payload"]["job_ids"] == [101, 102]
    assert "job_id" not in calls[0]["payload"]
    assert "trace_id" not in calls[0]["payload"]


@pytest.mark.asyncio
async def test_emit_failed_run_updated(monkeypatch):
    calls = []

    async def _fake_emit_run_event(*, db, run_id, event_type, payload):
        calls.append(
            {
                "db": db,
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _fake_emit_run_event)

    db = object()
    await emit_failed_run_updated(
        db=db,
        run_id=7,
        fiche_id=11,
        thread_id=15,
        owner_id=23,
        finished_at_iso="2026-03-03T12:00:00+00:00",
        duration_ms=910,
        error="boom",
    )

    assert calls == [
        {
            "db": db,
            "run_id": 7,
            "event_type": "run_updated",
            "payload": {
                "fiche_id": 11,
                "status": "failed",
                "finished_at": "2026-03-03T12:00:00+00:00",
                "duration_ms": 910,
                "error": "boom",
                "thread_id": 15,
                "owner_id": 23,
            },
        }
    ]


@pytest.mark.asyncio
async def test_emit_success_run_updated(monkeypatch):
    calls = []

    async def _fake_emit_run_event(*, db, run_id, event_type, payload):
        calls.append(
            {
                "db": db,
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _fake_emit_run_event)

    db = object()
    await emit_success_run_updated(
        db=db,
        run_id=13,
        fiche_id=21,
        thread_id=34,
        owner_id=55,
        finished_at_iso="2026-03-03T13:00:00+00:00",
        duration_ms=222,
    )

    assert calls == [
        {
            "db": db,
            "run_id": 13,
            "event_type": "run_updated",
            "payload": {
                "fiche_id": 21,
                "status": "success",
                "finished_at": "2026-03-03T13:00:00+00:00",
                "duration_ms": 222,
                "thread_id": 34,
                "owner_id": 55,
            },
        }
    ]


@pytest.mark.asyncio
async def test_emit_cancelled_run_updated(monkeypatch):
    calls = []

    async def _fake_emit_run_event(*, db, run_id, event_type, payload):
        calls.append(
            {
                "db": db,
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _fake_emit_run_event)

    db = object()
    await emit_cancelled_run_updated(
        db=db,
        run_id=14,
        fiche_id=22,
        thread_id=35,
        owner_id=56,
        finished_at_iso="2026-03-03T13:05:00+00:00",
        duration_ms=333,
    )

    assert calls == [
        {
            "db": db,
            "run_id": 14,
            "event_type": "run_updated",
            "payload": {
                "fiche_id": 22,
                "status": "cancelled",
                "finished_at": "2026-03-03T13:05:00+00:00",
                "duration_ms": 333,
                "thread_id": 35,
                "owner_id": 56,
            },
        }
    ]


@pytest.mark.asyncio
async def test_emit_error_event_and_close_stream(monkeypatch):
    event_calls = []
    stream_calls = []

    async def _fake_emit_run_event(*, db, run_id, event_type, payload):
        event_calls.append(
            {
                "db": db,
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    async def _fake_emit_stream_control(db, run, action, reason, owner_id, ttl_ms=None):
        stream_calls.append(
            {
                "db": db,
                "run_id": run.id,
                "action": action,
                "reason": reason,
                "owner_id": owner_id,
                "ttl_ms": ttl_ms,
            }
        )

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _fake_emit_run_event)
    monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control", _fake_emit_stream_control)

    db = object()
    run = SimpleNamespace(id=88)

    await emit_error_event_and_close_stream(
        db=db,
        run=run,
        thread_id=17,
        owner_id=24,
        message="bad thing",
        trace_id="trace-err",
        fiche_id=31,
        debug_url="/oikos/88",
    )

    assert event_calls == [
        {
            "db": db,
            "run_id": 88,
            "event_type": "error",
            "payload": {
                "thread_id": 17,
                "message": "bad thing",
                "status": "error",
                "owner_id": 24,
                "trace_id": "trace-err",
                "fiche_id": 31,
                "debug_url": "/oikos/88",
            },
        }
    ]
    assert stream_calls == [
        {
            "db": db,
            "run_id": 88,
            "action": "close",
            "reason": "error",
            "owner_id": 24,
            "ttl_ms": None,
        }
    ]


@pytest.mark.asyncio
async def test_emit_stream_control_keeps_stream_open_when_commiss_pending(monkeypatch):
    calls = []

    async def _fake_emit_stream_control(db, run, action, reason, owner_id, ttl_ms=None):
        calls.append(
            {
                "db": db,
                "run_id": run.id,
                "action": action,
                "reason": reason,
                "owner_id": owner_id,
                "ttl_ms": ttl_ms,
            }
        )

    monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control", _fake_emit_stream_control)

    db = _DummyDb(pending_count=2)
    run = SimpleNamespace(id=42)

    pending = await emit_stream_control_for_pending_commiss(db, run, owner_id=7)

    assert pending == 2
    assert calls == [
        {
            "db": db,
            "run_id": 42,
            "action": "keep_open",
            "reason": "commiss_pending",
            "owner_id": 7,
            "ttl_ms": 120_000,
        }
    ]


@pytest.mark.asyncio
async def test_emit_stream_control_closes_when_no_commiss_pending(monkeypatch):
    calls = []

    async def _fake_emit_stream_control(db, run, action, reason, owner_id, ttl_ms=None):
        calls.append(
            {
                "db": db,
                "run_id": run.id,
                "action": action,
                "reason": reason,
                "owner_id": owner_id,
                "ttl_ms": ttl_ms,
            }
        )

    monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control", _fake_emit_stream_control)

    db = _DummyDb(pending_count=0)
    run = SimpleNamespace(id=99)

    pending = await emit_stream_control_for_pending_commiss(db, run, owner_id=11)

    assert pending == 0
    assert calls == [
        {
            "db": db,
            "run_id": 99,
            "action": "close",
            "reason": "all_complete",
            "owner_id": 11,
            "ttl_ms": None,
        }
    ]
