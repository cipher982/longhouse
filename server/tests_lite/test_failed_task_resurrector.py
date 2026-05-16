"""Tests for the failed-task resurrector ("house cleaner").

Resurrects terminally-failed summary/embedding ingest tasks so a model swap
or transient outage doesn't leave thousands of rows stuck forever.
"""

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services import ingest_task_queue as itq
from zerg.services.ingest_task_queue import RESURRECT_EXHAUSTED_ERROR
from zerg.services.ingest_task_queue import RESURRECT_MAX_CYCLES
from zerg.services.ingest_task_queue import _resurrect_failed_tasks_atomic
from zerg.services.ingest_task_queue import run_failed_task_resurrector
from zerg.services.write_serializer import WriteSerializer


def _make_db(tmp_path, name: str):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _add_session(
    db,
    *,
    transcript_revision: int = 0,
    summary_revision: int = 0,
    embedding_revision: int = 0,
    needs_embedding: int = 1,
) -> AgentSession:
    session = AgentSession(
        provider="codex",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=transcript_revision,
        summary_revision=summary_revision,
        embedding_revision=embedding_revision,
        needs_embedding=needs_embedding,
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
    status: str = "failed",
    updated_at: datetime | None = None,
    resurrection_count: int = 0,
    error: str | None = "boom",
    attempts: int = 3,
) -> SessionTask:
    now = updated_at or datetime.now(timezone.utc)
    task = SessionTask(
        session_id=session_id,
        task_type=task_type,
        status=status,
        attempts=attempts,
        resurrection_count=resurrection_count,
        error=error,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_failed_older_than_gate_is_resurrected(tmp_path):
    factory = _make_db(tmp_path, "older_than_gate.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    old = _add_task(
        db,
        str(session.id),
        "summary",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    count = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=30)
    db.commit()
    db.expire_all()

    assert count == 1
    task = db.get(SessionTask, old.id)
    assert task.status == "pending"
    assert task.attempts == 0
    assert task.error is None
    assert task.resurrection_count == 1
    db.close()


def test_failed_inside_gate_is_left_alone(tmp_path):
    factory = _make_db(tmp_path, "inside_gate.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    fresh = _add_task(
        db,
        str(session.id),
        "summary",
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    count = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=30)
    db.commit()
    db.expire_all()

    assert count == 0
    task = db.get(SessionTask, fresh.id)
    assert task.status == "failed"
    assert task.resurrection_count == 0
    db.close()


def test_already_current_session_is_closed_not_repended(tmp_path):
    factory = _make_db(tmp_path, "already_current.db")
    db = factory()
    # Summary already up-to-date for transcript revision.
    session = _add_session(db, transcript_revision=4, summary_revision=4)
    failed = _add_task(db, str(session.id), "summary")

    count = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=None)
    db.commit()
    db.expire_all()

    assert count == 0
    task = db.get(SessionTask, failed.id)
    assert task.status == "done"
    assert task.error is None
    # Resurrection budget unchanged — no resurrection happened.
    assert task.resurrection_count == 0
    db.close()


def test_active_peer_blocks_resurrection(tmp_path):
    factory = _make_db(tmp_path, "active_peer.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    failed = _add_task(db, str(session.id), "summary")
    # Sibling pending row created later by the normal enqueue path.
    pending = _add_task(db, str(session.id), "summary", status="pending", error=None)

    count = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=None)
    db.commit()
    db.expire_all()

    assert count == 0
    assert db.get(SessionTask, failed.id).status == "failed"
    assert db.get(SessionTask, pending.id).status == "pending"
    # Exactly one pending row for this (session, type).
    pending_rows = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == str(session.id),
            SessionTask.task_type == "summary",
            SessionTask.status == "pending",
        )
        .count()
    )
    assert pending_rows == 1
    db.close()


def test_resurrection_cap_terminates_after_five_cycles(tmp_path):
    factory = _make_db(tmp_path, "resurrection_cap.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    # Task already at the cap — next attempt is the sixth and must terminate.
    capped = _add_task(
        db,
        str(session.id),
        "summary",
        resurrection_count=RESURRECT_MAX_CYCLES,
    )

    count = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=None)
    db.commit()
    db.expire_all()

    assert count == 0
    task = db.get(SessionTask, capped.id)
    assert task.status == "failed"
    assert task.error == RESURRECT_EXHAUSTED_ERROR
    assert task.resurrection_count == RESURRECT_MAX_CYCLES
    db.close()


def test_batch_cap_limits_per_cycle_work(tmp_path):
    factory = _make_db(tmp_path, "batch_cap.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    # Use distinct task_types/positions so dedup doesn't collapse them — but
    # we can only have one (session, summary) failed without a peer dedup
    # collision. Use multiple sessions instead.
    failed_ids: list[str] = []
    for _ in range(5):
        s = _add_session(db, transcript_revision=2, summary_revision=0)
        t = _add_task(db, str(s.id), "summary")
        failed_ids.append(t.id)

    count = _resurrect_failed_tasks_atomic(db, batch_size=2, time_gate_minutes=None)
    db.commit()
    db.expire_all()

    assert count == 2
    pending_count = (
        db.query(SessionTask)
        .filter(SessionTask.id.in_(failed_ids), SessionTask.status == "pending")
        .count()
    )
    assert pending_count == 2
    db.close()


def test_startup_backfill_has_no_time_gate(tmp_path):
    factory = _make_db(tmp_path, "startup_backfill.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    # Brand-new failed row (just failed seconds ago) — would be skipped by
    # the steady-state 30-minute gate, but startup backfill grabs it.
    fresh = _add_task(
        db,
        str(session.id),
        "summary",
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )

    count = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=None)
    db.commit()
    db.expire_all()

    assert count == 1
    assert db.get(SessionTask, fresh.id).status == "pending"
    db.close()


def test_resurrector_is_idempotent_back_to_back(tmp_path):
    factory = _make_db(tmp_path, "idempotent.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    failed = _add_task(db, str(session.id), "summary")

    first = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=None)
    db.commit()
    second = _resurrect_failed_tasks_atomic(db, batch_size=100, time_gate_minutes=None)
    db.commit()
    db.expire_all()

    assert first == 1
    # Second pass: row is now pending, so there's nothing failed to resurrect.
    assert second == 0
    task = db.get(SessionTask, failed.id)
    assert task.status == "pending"
    assert task.resurrection_count == 1
    db.close()


def test_concurrent_enqueue_and_resurrect_yields_one_pending_row(tmp_path, monkeypatch):
    """Atomicity: serializer ordering means the second caller sees the first's state.

    Both writes go through the same WriteSerializer lock. Whichever lands
    first wins; the second sees the resulting state and no-ops accordingly.
    Net result: exactly one pending row.
    """
    factory = _make_db(tmp_path, "concurrent.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    failed = _add_task(db, str(session.id), "summary")
    db.close()

    serializer = WriteSerializer()
    serializer.configure(factory)
    monkeypatch.setattr(itq, "get_write_serializer", lambda: serializer)

    from zerg.services.ingest_task_queue import _enqueue_if_not_active

    async def run_both() -> None:
        # Resurrect first, then enqueue: enqueue must see the now-pending row
        # (created by the resurrect flip) and skip — never produce a duplicate.
        await serializer.execute(
            lambda db: _resurrect_failed_tasks_atomic(
                db, batch_size=10, time_gate_minutes=None
            ),
            label="task-resurrect",
        )
        await serializer.execute(
            lambda db: _enqueue_if_not_active(db, str(session.id), "summary"),
            label="ingest",
        )

    asyncio.run(run_both())

    db = factory()
    rows = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == str(session.id),
            SessionTask.task_type == "summary",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].id == failed.id
    assert rows[0].status == "pending"
    db.close()


def test_concurrent_enqueue_then_resurrect_yields_one_pending_row(tmp_path, monkeypatch):
    """Reverse order: enqueue first creates a pending peer, resurrect must skip."""
    factory = _make_db(tmp_path, "concurrent_reverse.db")
    db = factory()
    session = _add_session(db, transcript_revision=2, summary_revision=0)
    failed = _add_task(db, str(session.id), "summary")
    db.close()

    serializer = WriteSerializer()
    serializer.configure(factory)
    monkeypatch.setattr(itq, "get_write_serializer", lambda: serializer)

    from zerg.services.ingest_task_queue import _enqueue_if_not_active

    async def run_both() -> None:
        await serializer.execute(
            lambda db: _enqueue_if_not_active(db, str(session.id), "summary"),
            label="ingest",
        )
        await serializer.execute(
            lambda db: _resurrect_failed_tasks_atomic(
                db, batch_size=10, time_gate_minutes=None
            ),
            label="task-resurrect",
        )

    asyncio.run(run_both())

    db = factory()
    rows = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == str(session.id),
            SessionTask.task_type == "summary",
        )
        .all()
    )
    pending = [r for r in rows if r.status == "pending"]
    assert len(pending) == 1
    # The original failed row stays failed because a pending peer existed.
    assert db.get(SessionTask, failed.id).status == "failed"
    db.close()


def test_run_failed_task_resurrector_drains_startup_backlog(tmp_path, monkeypatch):
    """Smoke test: run_failed_task_resurrector pages through a backlog at startup."""
    factory = _make_db(tmp_path, "drain_startup.db")
    db = factory()
    failed_ids: list[str] = []
    for _ in range(3):
        s = _add_session(db, transcript_revision=2, summary_revision=0)
        t = _add_task(db, str(s.id), "summary")
        failed_ids.append(t.id)
    db.close()

    serializer = WriteSerializer()
    serializer.configure(factory)
    monkeypatch.setattr(itq, "get_write_serializer", lambda: serializer)
    # Skip the inter-batch sleep so the test is fast.
    monkeypatch.setattr(itq, "RESURRECT_STARTUP_PACE_SECONDS", 0)

    async def run_briefly() -> None:
        task = asyncio.create_task(
            run_failed_task_resurrector(poll_seconds=3600, batch_size=2)
        )
        # Yield until the startup backfill drains; then cancel.
        for _ in range(50):
            await asyncio.sleep(0.01)
            db_check = factory()
            try:
                pending = (
                    db_check.query(SessionTask)
                    .filter(SessionTask.status == "pending")
                    .count()
                )
            finally:
                db_check.close()
            if pending == 3:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())

    db = factory()
    pending_count = (
        db.query(SessionTask)
        .filter(SessionTask.id.in_(failed_ids), SessionTask.status == "pending")
        .count()
    )
    assert pending_count == 3
    db.close()
