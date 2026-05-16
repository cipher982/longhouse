"""Tests for the derived `summary_status` field on session list payloads.

States:
  ready       — session.summary is non-empty
  pending     — no summary, latest summary task is pending|running
  failed      — no summary, latest summary task is failed AND
                resurrection_count >= SUMMARY_TERMINAL_RESURRECTION_COUNT
  unavailable — no summary AND (no task row OR user_messages < 2)

Tiebreaker: ready > pending > failed > unavailable.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from sqlalchemy import event as sa_event

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services.session_response_projection import SUMMARY_TERMINAL_RESURRECTION_COUNT
from zerg.services.session_response_projection import build_session_response_list
from zerg.services.session_response_projection import derive_summary_status
from zerg.services.agents_store import AgentsStore


def _make_db(tmp_path):
    db_path = tmp_path / "test_summary_status.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    summary=None,
    user_messages=5,
    started_at=None,
):
    session = AgentSession(
        provider="claude",
        environment="production",
        project="test-summary-status",
        started_at=started_at or datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=user_messages,
        assistant_messages=user_messages,
        tool_calls=0,
        summary=summary,
        summary_title="Title" if summary else None,
        summary_event_count=10 if summary else 0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_summary_task(
    db,
    session,
    *,
    status: str,
    resurrection_count: int = 0,
    updated_at: datetime | None = None,
):
    task = SessionTask(
        id=str(uuid4()),
        session_id=str(session.id),
        task_type="summary",
        status=status,
        attempts=1,
        max_attempts=5,
        resurrection_count=resurrection_count,
    )
    db.add(task)
    db.commit()
    if updated_at is not None:
        # SQLAlchemy onupdate would clobber a manual set on commit; explicit UPDATE
        # avoids that.
        db.query(SessionTask).filter(SessionTask.id == task.id).update(
            {"updated_at": updated_at}
        )
        db.commit()
    db.refresh(task)
    return task


def _build(db, session) -> dict:
    store = AgentsStore(db)
    [resp] = build_session_response_list(db=db, store=store, sessions=[session])
    return resp.model_dump()


# ---------------------------------------------------------------------------
# Pure unit tests on derive_summary_status — exhaustive truth table
# ---------------------------------------------------------------------------


def test_derive_ready_when_summary_present():
    assert derive_summary_status(summary="x", user_messages=0, task_state="pending") == "ready"
    assert derive_summary_status(summary="x", user_messages=0, task_state="failed") == "ready"


def test_derive_ready_beats_failed():
    """Tiebreaker: ready wins even with a failed task row."""
    assert derive_summary_status(summary="real summary", user_messages=10, task_state="failed") == "ready"


def test_derive_pending_when_task_active():
    assert derive_summary_status(summary=None, user_messages=10, task_state="pending") == "pending"


def test_derive_failed_when_task_terminal():
    assert derive_summary_status(summary=None, user_messages=10, task_state="failed") == "failed"


def test_derive_unavailable_when_no_task_and_low_content():
    assert derive_summary_status(summary=None, user_messages=1, task_state=None) == "unavailable"


def test_derive_unavailable_when_no_task_with_enough_content():
    """Bench gap: enough turns, no task row yet — show as unavailable, not pending."""
    assert derive_summary_status(summary=None, user_messages=10, task_state=None) == "unavailable"


def test_derive_unavailable_when_summary_blank_string():
    assert derive_summary_status(summary="   ", user_messages=10, task_state=None) == "unavailable"


# ---------------------------------------------------------------------------
# End-to-end through build_session_response_list
# ---------------------------------------------------------------------------


def test_session_with_summary_renders_ready(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary="Done.")
        out = _build(db, s)
        assert out["summary_status"] == "ready"
    finally:
        db.close()


def test_session_with_pending_task_renders_pending(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None)
        _seed_summary_task(db, s, status="pending")
        out = _build(db, s)
        assert out["summary_status"] == "pending"
    finally:
        db.close()


def test_session_with_running_task_renders_pending(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None)
        _seed_summary_task(db, s, status="running")
        out = _build(db, s)
        assert out["summary_status"] == "pending"
    finally:
        db.close()


def test_session_with_terminal_failed_task_renders_failed(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None)
        _seed_summary_task(
            db,
            s,
            status="failed",
            resurrection_count=SUMMARY_TERMINAL_RESURRECTION_COUNT,
        )
        out = _build(db, s)
        assert out["summary_status"] == "failed"
    finally:
        db.close()


def test_session_with_non_terminal_failed_task_renders_unavailable(tmp_path):
    """A failed task that hasn't exhausted resurrection cycles still has a
    chance of being retried, so we don't show 'failed' yet."""
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None)
        _seed_summary_task(db, s, status="failed", resurrection_count=2)
        out = _build(db, s)
        assert out["summary_status"] == "unavailable"
    finally:
        db.close()


def test_session_without_task_renders_unavailable(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None, user_messages=1)
        out = _build(db, s)
        assert out["summary_status"] == "unavailable"
    finally:
        db.close()


def test_summary_wins_over_failed_task(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary="Already summarized.")
        _seed_summary_task(
            db,
            s,
            status="failed",
            resurrection_count=SUMMARY_TERMINAL_RESURRECTION_COUNT,
        )
        out = _build(db, s)
        assert out["summary_status"] == "ready"
    finally:
        db.close()


def test_latest_task_wins_when_multiple_rows(tmp_path):
    """A historical 'failed' row plus a fresh 'pending' row → pending."""
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        _seed_summary_task(
            db, s, status="failed", resurrection_count=5, updated_at=old
        )
        _seed_summary_task(db, s, status="pending")
        out = _build(db, s)
        assert out["summary_status"] == "pending"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Single-query batching (no N+1)
# ---------------------------------------------------------------------------


def test_batched_summary_status_resolves_with_single_query(tmp_path):
    """50 sessions → exactly one summary-task lookup, regardless of session count."""
    engine, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        sessions = [_seed_session(db, summary=None) for _ in range(50)]
        # Sprinkle some task states across the batch.
        for i, sess in enumerate(sessions):
            if i % 4 == 0:
                _seed_summary_task(db, sess, status="pending")
            elif i % 4 == 1:
                _seed_summary_task(
                    db, sess, status="failed", resurrection_count=SUMMARY_TERMINAL_RESURRECTION_COUNT
                )
            elif i % 4 == 2:
                _seed_summary_task(db, sess, status="failed", resurrection_count=1)
            # else: no task row → unavailable

        # Count queries against the session_tasks table during the build.
        executed_sql: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            executed_sql.append(statement)

        sa_event.listen(engine, "before_cursor_execute", _capture)
        try:
            store = AgentsStore(db)
            results = build_session_response_list(db=db, store=store, sessions=sessions)
        finally:
            sa_event.remove(engine, "before_cursor_execute", _capture)

        assert len(results) == 50
        # Sanity check: distribution matches what we seeded.
        statuses = [r.summary_status for r in results]
        assert statuses.count("pending") == sum(1 for i in range(50) if i % 4 == 0)
        assert statuses.count("failed") == sum(1 for i in range(50) if i % 4 == 1)
        # Both `i % 4 == 2` (non-terminal failed) and `i % 4 == 3` (no row) → unavailable.
        assert statuses.count("unavailable") >= sum(
            1 for i in range(50) if i % 4 in (2, 3)
        )

        # The summary task lookup must be a single query, not 50.
        task_queries = [
            sql for sql in executed_sql if "session_tasks" in sql.lower() and "summary" in sql.lower()
        ]
        # We expect ONE batched join (via load_summary_status_map). Allow at most
        # 1 to keep the contract crisp; anything higher is N+1 regression.
        assert len(task_queries) <= 1, (
            f"Expected ≤1 session_tasks query, got {len(task_queries)}:\n"
            + "\n".join(task_queries)
        )
    finally:
        db.close()
