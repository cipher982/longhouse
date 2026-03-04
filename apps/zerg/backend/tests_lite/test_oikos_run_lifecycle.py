"""Tests for shared Oikos run lifecycle helpers."""

from types import SimpleNamespace

import pytest

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
