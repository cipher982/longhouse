"""Tests for the derived `summary_status` field on session list payloads.

States:
  ready       — session.summary is non-empty after trimming
  pending     — no summary, enough content, and summary_revision < transcript_revision
  unavailable — no summary and content is too small, transcript_revision is 0,
                or summary_revision >= transcript_revision
  failed      — reserved for a future explicit terminal enrichment field

Old SessionTask(summary) rows are ignored. Revision lag is the work contract.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy import event as sa_event

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services.agents_store import AgentsStore
from zerg.services.session_response_projection import build_session_response_list
from zerg.services.session_response_projection import derive_summary_status


def _make_db(tmp_path):
    db_path = tmp_path / "test_summary_status.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    summary=None,
    user_messages=2,
    assistant_messages=2,
    transcript_revision=2,
    summary_revision=0,
    started_at=None,
):
    session = AgentSession(
        provider="claude",
        environment="production",
        project="test-summary-status",
        started_at=started_at or datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_calls=0,
        summary=summary,
        summary_title="Title" if summary and summary.strip() else None,
        summary_event_count=10 if summary and summary.strip() else 0,
        transcript_revision=transcript_revision,
        summary_revision=summary_revision,
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
    db.refresh(task)
    return task


def _build(db, session) -> dict:
    store = AgentsStore(db)
    [resp] = build_session_response_list(db=db, store=store, sessions=[session])
    return resp.model_dump()


# ---------------------------------------------------------------------------
# Pure unit tests on derive_summary_status — revision-lag truth table
# ---------------------------------------------------------------------------


def test_derive_ready_when_summary_present():
    assert (
        derive_summary_status(
            summary="x",
            user_messages=0,
            assistant_messages=0,
            transcript_revision=0,
            summary_revision=0,
        )
        == "ready"
    )


def test_derive_ready_when_summary_present_even_if_revision_lags():
    assert (
        derive_summary_status(
            summary="real summary",
            user_messages=10,
            assistant_messages=10,
            transcript_revision=5,
            summary_revision=1,
        )
        == "ready"
    )


def test_derive_pending_when_revision_lags_with_enough_content():
    assert (
        derive_summary_status(
            summary=None,
            user_messages=1,
            assistant_messages=1,
            transcript_revision=3,
            summary_revision=2,
        )
        == "pending"
    )


def test_derive_unavailable_when_summary_blank_string():
    assert (
        derive_summary_status(
            summary="   ",
            user_messages=10,
            assistant_messages=10,
            transcript_revision=2,
            summary_revision=2,
        )
        == "unavailable"
    )


def test_derive_unavailable_when_revision_zero():
    assert (
        derive_summary_status(
            summary=None,
            user_messages=10,
            assistant_messages=10,
            transcript_revision=0,
            summary_revision=0,
        )
        == "unavailable"
    )


def test_derive_unavailable_when_content_too_small():
    assert (
        derive_summary_status(
            summary=None,
            user_messages=1,
            assistant_messages=0,
            transcript_revision=3,
            summary_revision=0,
        )
        == "unavailable"
    )


def test_derive_unavailable_when_empty_summary_is_current():
    assert (
        derive_summary_status(
            summary=None,
            user_messages=10,
            assistant_messages=10,
            transcript_revision=3,
            summary_revision=3,
        )
        == "unavailable"
    )


# ---------------------------------------------------------------------------
# End-to-end through build_session_response_list
# ---------------------------------------------------------------------------


def test_session_with_summary_renders_ready(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary="Done.", transcript_revision=3, summary_revision=1)
        out = _build(db, s)
        assert out["summary_status"] == "ready"
    finally:
        db.close()


def test_session_with_stale_summary_revision_renders_pending(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None, transcript_revision=3, summary_revision=2)
        out = _build(db, s)
        assert out["summary_status"] == "pending"
    finally:
        db.close()


def test_session_with_current_empty_summary_renders_unavailable(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None, transcript_revision=3, summary_revision=3)
        out = _build(db, s)
        assert out["summary_status"] == "unavailable"
    finally:
        db.close()


def test_session_with_too_little_content_renders_unavailable(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(
            db,
            summary=None,
            user_messages=1,
            assistant_messages=0,
            transcript_revision=3,
            summary_revision=0,
        )
        out = _build(db, s)
        assert out["summary_status"] == "unavailable"
    finally:
        db.close()


def test_session_with_zero_revision_renders_unavailable(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None, transcript_revision=0, summary_revision=0)
        out = _build(db, s)
        assert out["summary_status"] == "unavailable"
    finally:
        db.close()


def test_old_pending_summary_task_is_ignored(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None, transcript_revision=3, summary_revision=3)
        _seed_summary_task(db, s, status="pending")
        out = _build(db, s)
        assert out["summary_status"] == "unavailable"
    finally:
        db.close()


def test_old_failed_summary_task_is_ignored(tmp_path):
    _, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        s = _seed_session(db, summary=None, transcript_revision=3, summary_revision=2)
        _seed_summary_task(db, s, status="failed", resurrection_count=99)
        out = _build(db, s)
        assert out["summary_status"] == "pending"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Batching — summary status must not touch session_tasks.
# ---------------------------------------------------------------------------


def test_batched_summary_status_does_not_query_session_tasks(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        sessions = [
            _seed_session(db, summary=None, transcript_revision=3, summary_revision=(0 if i % 2 == 0 else 3))
            for i in range(50)
        ]
        for sess in sessions:
            _seed_summary_task(db, sess, status="pending")

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
        statuses = [r.summary_status for r in results]
        assert statuses.count("pending") == 25
        assert statuses.count("unavailable") == 25

        task_queries = [sql for sql in executed_sql if "session_tasks" in sql.lower()]
        assert task_queries == []
    finally:
        db.close()
