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
from zerg.services.agents.kernel_backfill import backfill_child_thread_ids
from zerg.services.agents.kernel_backfill import backfill_root_threads
from zerg.services.agents.kernel_backfill import backfill_runs_and_connections
from zerg.services.agents.kernel_backfill import backfill_session_identity_kernel


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
        # Session-identity-kernel cleanup: ``provider_session_id`` is no
        # longer a real column. Each session synthesizes one as ``str(self.id)``,
        # so the backfill mirrors an alias for every session.
        assert report["aliases_created"] == 3

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

        # Every session now gets a synthesized provider_session_id alias.
        assert db.query(SessionThreadAlias).count() == 3


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


def test_backfill_reuses_preexisting_primary_thread(tmp_path):
    """If a primary thread already exists for a session, the backfill must
    point sessions.primary_thread_id at it instead of creating a second one.
    """
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        s = _make_session(db, provider_session_id="codex-1")
        existing = SessionThread(
            session_id=s.id, provider="codex", branch_kind="root", is_primary=1
        )
        db.add(existing)
        db.flush()
        existing_id = existing.id
        db.commit()

        report = backfill_root_threads(db)
        db.commit()

        assert report["threads_created"] == 0
        assert report["primary_pointers_set"] == 1
        assert db.query(SessionThread).count() == 1
        db.refresh(s)
        assert s.primary_thread_id == existing_id


def test_backfill_leaves_existing_primary_pointer_untouched(tmp_path):
    """When sessions.primary_thread_id already points at the right thread,
    the backfill should be a no-op for that session.
    """
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        s = _make_session(db, provider_session_id="codex-1")
        thread = SessionThread(
            session_id=s.id, provider="codex", branch_kind="root", is_primary=1
        )
        db.add(thread)
        db.flush()
        s.primary_thread_id = thread.id
        db.commit()

        report = backfill_root_threads(db)
        db.commit()

        assert report["threads_created"] == 0
        assert report["primary_pointers_set"] == 0


def test_backfill_same_provider_session_id_on_different_sessions(tmp_path):
    """Two sessions with the same provider_session_id must each get their own
    alias row — aliases are evidence, not identity, and the per-thread
    uniqueness constraint scopes to thread.
    """
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        _make_session(db, provider_session_id="shared-codex")
        _make_session(db, provider_session_id="shared-codex")
        db.commit()

        backfill_root_threads(db)
        db.commit()

        assert db.query(SessionThreadAlias).count() == 2


def test_backfill_order_independent_final_state(tmp_path):
    """Final state must be identical whether sessions are created all-up-front
    or interleaved with backfill runs.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    engine_a = _engine(tmp_path / "a")
    engine_b = _engine(tmp_path / "b")
    Sa = sessionmaker(bind=engine_a)
    Sb = sessionmaker(bind=engine_b)

    # Path A: all sessions created, then one backfill.
    with Sa() as db:
        _make_session(db, provider_session_id="codex-1")
        _make_session(db, provider="claude", provider_session_id="claude-1")
        _make_session(db, provider="gemini")
        db.commit()
        backfill_root_threads(db)
        db.commit()
        a_threads = db.query(SessionThread).count()
        a_aliases = db.query(SessionThreadAlias).count()

    # Path B: interleaved create / backfill / create / backfill.
    with Sb() as db:
        _make_session(db, provider_session_id="codex-1")
        db.commit()
        backfill_root_threads(db)
        db.commit()
        _make_session(db, provider="claude", provider_session_id="claude-1")
        db.commit()
        backfill_root_threads(db)
        db.commit()
        _make_session(db, provider="gemini")
        db.commit()
        backfill_root_threads(db)
        db.commit()
        b_threads = db.query(SessionThread).count()
        b_aliases = db.query(SessionThreadAlias).count()

    assert a_threads == b_threads == 3
    # Session-identity-kernel cleanup: every session synthesizes a
    # provider_session_id (= str(self.id)), so the backfill mirrors three
    # aliases regardless of interleaving.
    assert a_aliases == b_aliases == 3


def test_backfill_child_thread_ids_stamps_legacy_rows(tmp_path):
    """Child rows created with thread_id=NULL should be stamped on backfill."""
    from zerg.models.agents import AgentEvent
    from zerg.models.agents import AgentSourceLine
    from zerg.models.agents import SessionInput

    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        s = _make_session(db, provider_session_id="codex-1")
        # Insert legacy children with NULL thread_id (Phase 0 shape).
        db.add(AgentEvent(session_id=s.id, role="user", content_text="hi", timestamp=datetime.now(timezone.utc)))
        db.add(
            AgentSourceLine(
                session_id=s.id,
                source_path="/tmp/p",
                source_offset=0,
                branch_id=0,
                revision=1,
                line_hash="h1",
                raw_json="{}",
            )
        )
        db.add(SessionInput(session_id=s.id, body="hello", intent="auto", status="queued"))
        db.commit()

        # Pre-thread backfill: child rows have NULL thread_id.
        from zerg.models.agents import AgentEvent as _E

        assert db.query(_E).filter(_E.thread_id.is_(None)).count() == 1

        backfill_root_threads(db)
        db.commit()
        report = backfill_child_thread_ids(db)
        db.commit()

        # Each table got one row stamped.
        assert report["events"] == 1
        assert report["source_lines"] == 1
        assert report["session_inputs"] == 1
        # Re-running is a no-op.
        again = backfill_child_thread_ids(db)
        assert sum(again.values()) == 0


def test_backfill_runs_and_connections_synthesizes_observe_only(tmp_path):
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun

    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        _make_session(db, provider_session_id="codex-1")
        _make_session(db, provider="claude", provider_session_id="claude-1")
        db.commit()
        backfill_root_threads(db)
        db.commit()

        report = backfill_runs_and_connections(db)
        db.commit()

        assert report["runs_created"] == 2
        assert report["connections_created"] == 2
        runs = db.query(SessionRun).all()
        assert {r.launch_origin for r in runs} == {"external_adopted"}
        conns = db.query(SessionConnection).all()
        assert {c.control_plane for c in conns} == {"log_tail"}
        assert all(c.acquisition_kind == "observe_only" for c in conns)
        # Re-running is a no-op.
        again = backfill_runs_and_connections(db)
        assert again["runs_created"] == 0
        assert again["connections_created"] == 0


def test_backfill_runs_does_not_displace_launcher_owned_run(tmp_path):
    from zerg.models.agents import SessionRun

    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        s = _make_session(db, provider_session_id="codex-1")
        db.commit()
        backfill_root_threads(db)
        db.commit()
        # Launcher already created a run.
        thread = db.query(SessionThread).filter(SessionThread.session_id == s.id).one()
        owned = SessionRun(thread_id=thread.id, provider="codex", launch_origin="longhouse_spawned")
        db.add(owned)
        db.commit()

        report = backfill_runs_and_connections(db)
        db.commit()

        # No new run created — launcher's run is reused.
        assert report["runs_created"] == 0
        assert db.query(SessionRun).count() == 1


def test_combined_backfill_is_idempotent_end_to_end(tmp_path):
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        _make_session(db, provider_session_id="codex-1")
        _make_session(db, provider="claude")
        db.commit()

        first = backfill_session_identity_kernel(db)
        db.commit()
        second = backfill_session_identity_kernel(db)
        db.commit()

        assert first["threads"]["threads_created"] == 2
        assert second["threads"]["threads_created"] == 0
        assert second["runs"]["runs_created"] == 0
        assert second["runs"]["connections_created"] == 0
        assert sum(second["children"].values()) == 0


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
