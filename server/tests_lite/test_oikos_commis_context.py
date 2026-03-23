"""Unit tests for extracted Oikos commis inbox-context helpers."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.models import CommisJob
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.models.models import User
from zerg.services.oikos_commis_context import RECENT_COMMIS_CONTEXT_MARKER
from zerg.services.oikos_commis_context import acknowledge_commis_jobs
from zerg.services.oikos_commis_context import build_recent_commis_context
from zerg.services.oikos_commis_context import cleanup_stale_commis_context


def _make_db(tmp_path):
    db_path = tmp_path / "test_oikos_commis_context.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, user_id=1):
    user = User(id=user_id, email=f"user{user_id}@local", role="ADMIN")
    db.add(user)
    db.commit()
    return user


def _utc_naive(dt: datetime) -> datetime:
    """Normalize timestamps to UTC naive to match model columns."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def test_build_recent_commis_context_includes_active_and_unread(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _seed_user(db, user_id=1)

        now = datetime.now(timezone.utc)
        active = CommisJob(
            owner_id=1,
            task="Investigate failing integration test",
            status="running",
            started_at=_utc_naive(now - timedelta(seconds=35)),
            created_at=_utc_naive(now - timedelta(minutes=1)),
        )
        unread = CommisJob(
            owner_id=1,
            task="Fix typo in README and push patch",
            status="success",
            acknowledged=False,
            created_at=_utc_naive(now - timedelta(minutes=2)),
            finished_at=_utc_naive(now - timedelta(seconds=40)),
        )
        db.add(active)
        db.add(unread)
        db.commit()

        context, jobs_to_ack = build_recent_commis_context(db, owner_id=1)

        assert context is not None
        assert RECENT_COMMIS_CONTEXT_MARKER in context
        assert "Active Commiss" in context
        assert "New Results (unread)" in context
        assert "read_commis_result(job_id)" in context
        assert jobs_to_ack == [unread.id]


def test_acknowledge_commis_jobs_marks_selected_rows(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _seed_user(db, user_id=1)

        job1 = CommisJob(owner_id=1, task="one", status="success", acknowledged=False)
        job2 = CommisJob(owner_id=1, task="two", status="failed", acknowledged=False)
        db.add_all([job1, job2])
        db.commit()

        acknowledge_commis_jobs(db, [job1.id])
        db.expire_all()

        refreshed1 = db.query(CommisJob).filter(CommisJob.id == job1.id).first()
        refreshed2 = db.query(CommisJob).filter(CommisJob.id == job2.id).first()
        assert refreshed1 is not None and bool(refreshed1.acknowledged) is True
        assert refreshed2 is not None and bool(refreshed2.acknowledged) is False


def test_cleanup_stale_commis_context_keeps_fresh_newest_message(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _seed_user(db, user_id=1)

        thread = Thread(title="Oikos", active=True)
        db.add(thread)
        db.commit()
        db.refresh(thread)

        now = datetime.now(timezone.utc)
        messages = [
            ThreadMessage(
                thread_id=thread.id,
                role="system",
                content=f"{RECENT_COMMIS_CONTEXT_MARKER}\nolder-1",
                sent_at=_utc_naive(now - timedelta(minutes=5)),
                processed=True,
            ),
            ThreadMessage(
                thread_id=thread.id,
                role="system",
                content=f"{RECENT_COMMIS_CONTEXT_MARKER}\nolder-2",
                sent_at=_utc_naive(now - timedelta(minutes=1)),
                processed=True,
            ),
            ThreadMessage(
                thread_id=thread.id,
                role="system",
                content=f"{RECENT_COMMIS_CONTEXT_MARKER}\nfresh",
                sent_at=_utc_naive(now - timedelta(seconds=2)),
                processed=True,
            ),
        ]
        db.add_all(messages)
        db.commit()

        deleted = cleanup_stale_commis_context(db, thread.id, min_age_seconds=5.0)
        db.commit()

        remaining = (
            db.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )

        assert deleted == 2
        assert len(remaining) == 1
        assert "fresh" in remaining[0].content
