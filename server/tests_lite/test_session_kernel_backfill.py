"""Idempotency and order-independence tests for the kernel backfill.

The Phase 1 backfill must converge to the same final state regardless of how
many times it is run, or how it interleaves with new session creation. Phase
3 will rely on this property to flip thread_id columns to NOT NULL safely.

See docs/specs/session-identity-kernel.md.
"""

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.services.agents.kernel_backfill import backfill_root_threads


def _engine(tmp_path):
    db_path = tmp_path / "kernel.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return engine


def _make_session(db, *, provider="codex", provider_session_id=None, project="zerg"):
    s = AgentSession(
        provider=provider,
        environment="test",
        project=project,
        device_id="dev",
        started_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        provider_session_id=provider_session_id,
    )
    db.add(s)
    db.flush()
    return s


def test_backfill_creates_one_thread_per_session(tmp_path):
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        s1 = _make_session(db, provider_session_id="codex-1")
        s2 = _make_session(db, provider="claude", provider_session_id="claude-1")
        s3 = _make_session(db, provider="gemini")  # no provider_session_id
        db.commit()

        report = backfill_root_threads(db)
        db.commit()

        assert report["sessions_seen"] == 3
        assert report["threads_created"] == 3
        assert report["primary_pointers_set"] == 3
        # Two of three sessions had a provider_session_id.
        assert report["aliases_created"] == 2

        # Each session has exactly one primary thread, pointed at by the
        # session row.
        for s in (s1, s2, s3):
            db.refresh(s)
            t = (
                db.query(SessionThread)
                .filter(SessionThread.session_id == s.id, SessionThread.is_primary == 1)
                .one()
            )
            assert s.primary_thread_id == t.id
            assert t.provider == s.provider
            assert t.branch_kind == "root"

        # gemini session has no alias row.
        assert db.query(SessionThreadAlias).count() == 2


def test_backfill_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        _make_session(db, provider_session_id="codex-1")
        _make_session(db, provider="claude", provider_session_id="claude-1")
        db.commit()

        first = backfill_root_threads(db)
        db.commit()
        second = backfill_root_threads(db)
        db.commit()
        third = backfill_root_threads(db)
        db.commit()

        # First run does the work; subsequent runs are no-ops.
        assert first["threads_created"] == 2
        assert second["threads_created"] == 0
        assert third["threads_created"] == 0
        assert second["primary_pointers_set"] == 0
        assert third["aliases_created"] == 0

        assert db.query(SessionThread).count() == 2
        assert db.query(SessionThreadAlias).count() == 2


def test_backfill_handles_new_sessions_after_first_run(tmp_path):
    """Order-independent: backfill runs, new sessions arrive, backfill runs
    again, end state is the same as if all sessions existed before the first
    backfill.
    """
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        _make_session(db, provider_session_id="codex-1")
        db.commit()

        backfill_root_threads(db)
        db.commit()

        # Add more sessions after backfill.
        _make_session(db, provider="claude", provider_session_id="claude-1")
        _make_session(db, provider="gemini", provider_session_id="gemini-1")
        db.commit()

        report = backfill_root_threads(db)
        db.commit()
        assert report["threads_created"] == 2
        assert report["aliases_created"] == 2

        assert db.query(SessionThread).count() == 3
        assert db.query(SessionThreadAlias).count() == 3
        # Every session row has a primary_thread_id set.
        for s in db.query(AgentSession).all():
            assert s.primary_thread_id is not None


def test_backfill_does_not_duplicate_aliases(tmp_path):
    """Re-running over a session with the same provider_session_id must not
    create a second alias row.
    """
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        _make_session(db, provider_session_id="codex-stable")
        db.commit()

        backfill_root_threads(db)
        db.commit()
        backfill_root_threads(db)
        db.commit()
        backfill_root_threads(db)
        db.commit()

        assert db.query(SessionThreadAlias).count() == 1
