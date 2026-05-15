"""Tests for revision-aware ingest task queue cleanup."""

import os
from datetime import datetime
from datetime import timezone

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.database import Base
from zerg.models.agents import SessionTask
from zerg.services.ingest_task_queue import close_current_pending_tasks


def _make_db(tmp_path, name: str):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _add_session(
    db,
    *,
    transcript_revision: int,
    summary_revision: int,
    embedding_revision: int,
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


def _add_task(db, session_id: str, task_type: str, *, status: str = "pending") -> SessionTask:
    task = SessionTask(
        session_id=session_id,
        task_type=task_type,
        status=status,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_close_current_pending_tasks_marks_only_current_sessions_done(tmp_path):
    factory = _make_db(tmp_path, "current_task_cleanup.db")

    db = factory()
    current = _add_session(
        db,
        transcript_revision=3,
        summary_revision=3,
        embedding_revision=3,
        needs_embedding=0,
    )
    stale = _add_session(
        db,
        transcript_revision=4,
        summary_revision=2,
        embedding_revision=1,
        needs_embedding=1,
    )

    current_summary = _add_task(db, str(current.id), "summary")
    current_embedding = _add_task(db, str(current.id), "embedding")
    stale_summary = _add_task(db, str(stale.id), "summary")
    stale_embedding = _add_task(db, str(stale.id), "embedding")
    done_task = _add_task(db, str(current.id), "summary", status="done")

    closed = close_current_pending_tasks(db)

    db.expire_all()
    assert closed == 2
    assert db.get(SessionTask, current_summary.id).status == "done"
    assert db.get(SessionTask, current_embedding.id).status == "done"
    assert db.get(SessionTask, stale_summary.id).status == "pending"
    assert db.get(SessionTask, stale_embedding.id).status == "pending"
    assert db.get(SessionTask, done_task.id).status == "done"
    db.close()


def test_close_current_pending_tasks_respects_limit(tmp_path):
    factory = _make_db(tmp_path, "current_task_cleanup_limit.db")

    db = factory()
    current = _add_session(
        db,
        transcript_revision=2,
        summary_revision=2,
        embedding_revision=2,
        needs_embedding=0,
    )
    first = _add_task(db, str(current.id), "summary")
    second = _add_task(db, str(current.id), "embedding")

    closed = close_current_pending_tasks(db, limit=1)

    db.expire_all()
    statuses = {db.get(SessionTask, first.id).status, db.get(SessionTask, second.id).status}
    assert closed == 1
    assert statuses == {"done", "pending"}
    db.close()
