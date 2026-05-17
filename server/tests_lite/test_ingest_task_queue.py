"""Tests for revision-aware ingest task queue cleanup."""

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.database import Base
from zerg.models.agents import SessionTask
from zerg.services.ingest_task_queue import ENQUEUE_DEDUP_WINDOW_HOURS
from zerg.services.ingest_task_queue import close_current_pending_tasks
from zerg.services.ingest_task_queue import enqueue_ingest_tasks


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


# ---------------------------------------------------------------------------
# Enqueue dedup against recent failed rows (Bug 2)
# ---------------------------------------------------------------------------


def _set_task_created_at(db, task_id, when: datetime) -> None:
    """Backdate created_at for dedup-window tests."""
    task = db.get(SessionTask, task_id)
    task.created_at = when
    task.updated_at = when
    db.commit()


def _count_tasks(db, session_id: str, task_type: str) -> int:
    return (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session_id, SessionTask.task_type == task_type)
        .count()
    )


def test_enqueue_skips_when_recent_failed_row_exists(tmp_path):
    """A recent failed row blocks new pile-up; the resurrector handles it."""
    factory = _make_db(tmp_path, "enqueue_dedup_failed.db")
    db = factory()
    session = _add_session(
        db, transcript_revision=2, summary_revision=0, embedding_revision=0
    )

    # Pre-existing failed summary + embedding rows from a prior ingest.
    failed_summary = _add_task(db, str(session.id), "summary", status="failed")
    failed_embed = _add_task(db, str(session.id), "embedding", status="failed")

    # New ingest activity attempts to enqueue again.
    enqueue_ingest_tasks(db, str(session.id))
    db.commit()

    # No duplicates added — the recent failed rows still cover this work.
    assert _count_tasks(db, str(session.id), "summary") == 1
    assert _count_tasks(db, str(session.id), "embedding") == 1
    # And the existing failed rows weren't disturbed.
    assert db.get(SessionTask, failed_summary.id).status == "failed"
    assert db.get(SessionTask, failed_embed.id).status == "failed"
    db.close()


def test_enqueue_allows_after_dedup_window_expires(tmp_path):
    """A failed row older than the dedup window doesn't block a fresh enqueue."""
    factory = _make_db(tmp_path, "enqueue_dedup_window_expired.db")
    db = factory()
    session = _add_session(
        db, transcript_revision=2, summary_revision=0, embedding_revision=0
    )

    old_when = datetime.now(timezone.utc) - timedelta(
        hours=ENQUEUE_DEDUP_WINDOW_HOURS + 1
    )
    old_failed = _add_task(db, str(session.id), "summary", status="failed")
    _set_task_created_at(db, old_failed.id, old_when)

    enqueue_ingest_tasks(db, str(session.id))
    db.commit()

    # New pending row added since the old failed row is outside the window.
    assert _count_tasks(db, str(session.id), "summary") == 2
    db.close()


def test_poison_session_does_not_accumulate_duplicates(tmp_path):
    """Repeated ingests on a stuck session must not pile up duplicate failed rows."""
    factory = _make_db(tmp_path, "poison_session_dedup.db")
    db = factory()
    session = _add_session(
        db, transcript_revision=5, summary_revision=0, embedding_revision=0
    )

    # First ingest attempt succeeded at enqueue but task later failed.
    _add_task(db, str(session.id), "summary", status="failed")
    _add_task(db, str(session.id), "embedding", status="failed")

    # Simulate 5 more ingest waves (transcript activity on a stuck session).
    for _ in range(5):
        enqueue_ingest_tasks(db, str(session.id))
        db.commit()

    assert _count_tasks(db, str(session.id), "summary") == 1
    assert _count_tasks(db, str(session.id), "embedding") == 1
    db.close()
