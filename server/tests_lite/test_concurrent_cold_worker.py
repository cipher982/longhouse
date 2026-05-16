"""Tests for concurrent cold-lane ingest worker + tiered timeouts.

The cold worker dispatches up to COLD_WORKER_CONCURRENCY task executions in
flight at once, gated by a semaphore. Hot worker stays serial. Per-attempt
timeouts ramp from fail-fast (30s) up to today's full budget (180s).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from datetime import timezone

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services import ingest_task_queue as itq
from zerg.services.ingest_task_queue import COLD_WORKER_CONCURRENCY
from zerg.services.ingest_task_queue import MAX_ATTEMPTS_DEFAULT
from zerg.services.ingest_task_queue import _timeout_for
from zerg.services.write_serializer import WriteSerializer


def _make_db(tmp_path, name: str):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _add_session(db) -> AgentSession:
    session = AgentSession(
        provider="codex",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=2,
        summary_revision=0,
        embedding_revision=0,
        needs_embedding=1,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_task(
    db,
    session_id: str,
    task_type: str,
    *,
    status: str = "pending",
    attempts: int = 0,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
) -> SessionTask:
    now = datetime.now(timezone.utc)
    task = SessionTask(
        session_id=session_id,
        task_type=task_type,
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Tiered timeout helper
# ---------------------------------------------------------------------------


def test_timeout_for_summary_ramps_per_attempt():
    assert _timeout_for("summary", 1) == 30.0
    assert _timeout_for("summary", 2) == 60.0
    assert _timeout_for("summary", 3) == 90.0
    assert _timeout_for("summary", 4) == 120.0
    assert _timeout_for("summary", 5) == 180.0
    # Past the array — clamps to last entry rather than crashing.
    assert _timeout_for("summary", 9) == 180.0
    # Pre-claim defensive lookup — clamps to first.
    assert _timeout_for("summary", 0) == 30.0


def test_timeout_for_embedding_is_fixed():
    for attempts in (1, 2, 3, 4, 5, 9):
        assert _timeout_for("embedding", attempts) == 30.0


def test_timeout_for_unknown_type_returns_none():
    assert _timeout_for("turn_loop", 1) is None
    assert _timeout_for("bogus", 3) is None


def test_hot_worker_lane_is_turn_loop_only():
    assert itq.HOT_INGEST_TASK_TYPES == ("turn_loop",)
    assert itq._is_hot_worker_lane(
        include_task_types=itq.HOT_INGEST_TASK_TYPES,
        exclude_task_types=None,
    )
    assert not itq._is_hot_worker_lane(
        include_task_types=(),
        exclude_task_types=None,
    )


# ---------------------------------------------------------------------------
# Concurrent dispatch
# ---------------------------------------------------------------------------


def _run_with_mock_impl(
    tmp_path,
    monkeypatch,
    *,
    impl,
    task_count: int,
    concurrency: int,
    db_name: str,
    settle_timeout: float = 5.0,
):
    """Spin up the worker against an in-memory factory + mocked _run_task_impl."""
    factory = _make_db(tmp_path, db_name)
    db = factory()
    task_ids: list[str] = []
    for _ in range(task_count):
        s = _add_session(db)
        t = _add_task(db, str(s.id), "summary")
        task_ids.append(t.id)
    db.close()

    serializer = WriteSerializer()
    serializer.configure(factory)
    monkeypatch.setattr(itq, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(itq, "_run_task_impl", impl)

    async def _drive():
        worker = asyncio.create_task(
            itq.run_ingest_task_worker(
                poll_seconds=0.05,
                worker_name="cold-test",
                exclude_task_types=itq.HOT_INGEST_TASK_TYPES,
                concurrency=concurrency,
            )
        )
        # Poll until all tasks have left "pending" (and "running") — done or failed.
        deadline = time.monotonic() + settle_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            db_check = factory()
            try:
                outstanding = (
                    db_check.query(SessionTask)
                    .filter(SessionTask.id.in_(task_ids))
                    .filter(SessionTask.status.in_(["pending", "running"]))
                    .count()
                )
            finally:
                db_check.close()
            if outstanding == 0:
                break
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    start = time.monotonic()
    asyncio.run(_drive())
    elapsed = time.monotonic() - start
    return factory, task_ids, elapsed


def test_cold_worker_runs_tasks_concurrently(tmp_path, monkeypatch):
    """8 tasks, concurrency=4, each sleeps 0.4s → wall < 8 * single (~3.2s).

    With a true semaphore, two waves of 4 → ~0.8s + scheduling overhead.
    Allow a generous buffer to keep this stable on busy CI.
    """
    in_flight_peak = {"value": 0}
    in_flight_now = {"value": 0}
    lock = asyncio.Lock()
    sleep_per_task = 0.4

    async def fake_impl(task_id, session_id, task_type):
        async with lock:
            in_flight_now["value"] += 1
            if in_flight_now["value"] > in_flight_peak["value"]:
                in_flight_peak["value"] = in_flight_now["value"]
        try:
            await asyncio.sleep(sleep_per_task)
        finally:
            async with lock:
                in_flight_now["value"] -= 1

    factory, task_ids, elapsed = _run_with_mock_impl(
        tmp_path,
        monkeypatch,
        impl=fake_impl,
        task_count=8,
        concurrency=COLD_WORKER_CONCURRENCY,
        db_name="cold_concurrent.db",
        settle_timeout=8.0,
    )

    # All 8 done.
    db = factory()
    try:
        done_count = (
            db.query(SessionTask)
            .filter(SessionTask.id.in_(task_ids), SessionTask.status == "done")
            .count()
        )
    finally:
        db.close()
    assert done_count == 8

    # Concurrency was actually exercised: peak >= 2, capped at 4.
    assert in_flight_peak["value"] >= 2
    assert in_flight_peak["value"] <= COLD_WORKER_CONCURRENCY

    # Wall clock significantly less than serial (8 * 0.4 = 3.2s). Two waves
    # of 4 ~= 0.8s of work; allow scheduling/poll slack but require beating
    # half of the serial budget.
    assert elapsed < (sleep_per_task * 8) * 0.6, f"elapsed={elapsed:.2f}s not better than serial"


def test_cold_worker_concurrency_one_is_serial(tmp_path, monkeypatch):
    """concurrency=1 (hot-lane shape) keeps in-flight peak at 1."""
    in_flight_peak = {"value": 0}
    in_flight_now = {"value": 0}
    lock = asyncio.Lock()

    async def fake_impl(task_id, session_id, task_type):
        async with lock:
            in_flight_now["value"] += 1
            if in_flight_now["value"] > in_flight_peak["value"]:
                in_flight_peak["value"] = in_flight_now["value"]
        try:
            await asyncio.sleep(0.1)
        finally:
            async with lock:
                in_flight_now["value"] -= 1

    factory, task_ids, _ = _run_with_mock_impl(
        tmp_path,
        monkeypatch,
        impl=fake_impl,
        task_count=4,
        concurrency=1,
        db_name="serial_lane.db",
        settle_timeout=5.0,
    )
    db = factory()
    try:
        done_count = (
            db.query(SessionTask)
            .filter(SessionTask.id.in_(task_ids), SessionTask.status == "done")
            .count()
        )
    finally:
        db.close()
    assert done_count == 4
    assert in_flight_peak["value"] == 1


# ---------------------------------------------------------------------------
# Tiered timeout end-to-end
# ---------------------------------------------------------------------------


def test_summary_attempt_one_times_out_at_thirty_seconds(tmp_path, monkeypatch):
    """Attempt 1 must time out fast (<= 30s). We assert the timeout *value*
    seen by _execute_task to keep the test wall-clock tiny.
    """
    captured: list[float] = []

    async def fake_wait_for(coro, timeout):
        captured.append(timeout)
        # Cancel the inner coro to keep the test fast — simulate timeout.
        task = asyncio.ensure_future(coro)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass
        raise asyncio.TimeoutError()

    async def fake_impl(task_id, session_id, task_type):
        # Long sleep so the fake_wait_for has something to cancel.
        await asyncio.sleep(60)

    factory = _make_db(tmp_path, "tiered_attempt_one.db")
    db = factory()
    s = _add_session(db)
    t = _add_task(db, str(s.id), "summary")
    task_id = t.id
    db.close()

    serializer = WriteSerializer()
    serializer.configure(factory)
    monkeypatch.setattr(itq, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(itq, "_run_task_impl", fake_impl)
    monkeypatch.setattr(itq.asyncio, "wait_for", fake_wait_for)

    async def _drive():
        # Attempt 1 → timeout should be 30s.
        await itq._execute_task(task_id, str(s.id), "summary", attempts=1)
        # Attempt 3 → timeout should be 90s.
        await itq._execute_task(task_id, str(s.id), "summary", attempts=3)
        # Attempt 5 → timeout should be 180s.
        await itq._execute_task(task_id, str(s.id), "summary", attempts=5)

    asyncio.run(_drive())
    assert captured == [30.0, 90.0, 180.0]


def test_embedding_timeout_stays_fixed_across_attempts(tmp_path, monkeypatch):
    captured: list[float] = []

    async def fake_wait_for(coro, timeout):
        captured.append(timeout)
        task = asyncio.ensure_future(coro)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass
        raise asyncio.TimeoutError()

    async def fake_impl(task_id, session_id, task_type):
        await asyncio.sleep(60)

    factory = _make_db(tmp_path, "embed_fixed.db")
    db = factory()
    s = _add_session(db)
    t = _add_task(db, str(s.id), "embedding")
    db.close()

    serializer = WriteSerializer()
    serializer.configure(factory)
    monkeypatch.setattr(itq, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(itq, "_run_task_impl", fake_impl)
    monkeypatch.setattr(itq.asyncio, "wait_for", fake_wait_for)

    async def _drive():
        for attempt in (1, 2, 3, 4, 5):
            await itq._execute_task(t.id, str(s.id), "embedding", attempts=attempt)

    asyncio.run(_drive())
    assert captured == [30.0, 30.0, 30.0, 30.0, 30.0]


# ---------------------------------------------------------------------------
# max_attempts default
# ---------------------------------------------------------------------------


def test_enqueue_sets_max_attempts_to_five(tmp_path, monkeypatch):
    factory = _make_db(tmp_path, "max_attempts_five.db")
    db = factory()
    s = _add_session(db)

    from zerg.services.ingest_task_queue import enqueue_ingest_tasks

    enqueue_ingest_tasks(db, str(s.id))
    db.commit()

    rows = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == str(s.id))
        .all()
    )
    assert len(rows) == 2
    for row in rows:
        assert row.max_attempts == MAX_ATTEMPTS_DEFAULT
    db.close()


def test_task_failing_four_times_keeps_going_fifth_marks_failed(tmp_path, monkeypatch):
    """A task with max_attempts=5: four failures → still pending; fifth → failed."""
    call_count = {"n": 0}

    async def fake_impl(task_id, session_id, task_type):
        call_count["n"] += 1
        raise RuntimeError(f"boom {call_count['n']}")

    factory = _make_db(tmp_path, "five_attempts.db")
    db = factory()
    s = _add_session(db)
    t = _add_task(db, str(s.id), "summary", max_attempts=5)
    db.close()

    serializer = WriteSerializer()
    serializer.configure(factory)
    monkeypatch.setattr(itq, "get_write_serializer", lambda: serializer)
    monkeypatch.setattr(itq, "_run_task_impl", fake_impl)

    async def _drive():
        # Manually drive 5 claim/execute cycles to verify the retry budget.
        for _ in range(5):
            tasks = await serializer.execute(
                lambda db: itq._claim_pending(
                    db,
                    1,
                    exclude_task_types=itq.HOT_INGEST_TASK_TYPES,
                ),
                label="task-claim",
            )
            assert tasks, "expected to claim a task each cycle until exhausted"
            tid, sid, ttype, attempts = tasks[0]
            await itq._execute_task(tid, sid, ttype, attempts)

    asyncio.run(_drive())

    db = factory()
    try:
        row = db.get(SessionTask, t.id)
        assert call_count["n"] == 5
        assert row.attempts == 5
        assert row.status == "failed"
        assert "boom" in (row.error or "")
    finally:
        db.close()
