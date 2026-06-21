"""Phase 1 schema sanity for the session identity kernel.

See docs/specs/session-identity-kernel.md. These tests assert that the new
tables exist with the expected columns and FK relationships, that the new
nullable columns on existing child tables roundtrip, and that
`Base.metadata.create_all` is sufficient (no imperative migration needed for
purely-additive Phase 1 work).
"""

from datetime import datetime
from datetime import timezone

from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionEdge
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias


def _engine(tmp_path):
    db_path = tmp_path / "kernel.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return engine


def test_kernel_tables_exist(tmp_path):
    engine = _engine(tmp_path)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for name in (
        "session_threads",
        "session_thread_aliases",
        "session_edges",
        "session_runs",
        "session_connections",
        "session_launch_attempts",
    ):
        assert name in tables, f"missing kernel table: {name}"


def test_child_tables_gained_thread_id(tmp_path):
    engine = _engine(tmp_path)
    insp = inspect(engine)
    expected = {
        "events": ["thread_id"],
        "source_lines": ["thread_id"],
        "session_observations": ["thread_id"],
        "session_runtime_state": ["thread_id", "run_id"],
        "session_turns": ["thread_id", "run_id"],
        "session_inputs": ["thread_id"],
        "sessions": ["primary_thread_id"],
    }
    for table, cols in expected.items():
        actual = {c["name"]: c for c in insp.get_columns(table)}
        for col in cols:
            assert col in actual, f"{table}.{col} missing"
            # Phase 1 columns must be nullable so existing rows are fine.
            assert actual[col]["nullable"] is True, f"{table}.{col} should be nullable in Phase 1"


def test_kernel_roundtrip_minimal(tmp_path):
    """Build the smallest end-to-end identity graph: session → thread → run →
    connection → launch_attempt → alias, plus thread_id-tagged child rows.
    """
    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 5, 21, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = AgentSession(
            provider="codex",
            environment="test",
            project="zerg",
            device_id="dev-machine",
            started_at=now,
            provider_session_id="s1",
        )
        db.add(session)
        db.flush()

        thread = SessionThread(
            session_id=session.id,
            provider="codex",
            branch_kind="root",
            is_primary=1,
        )
        db.add(thread)
        db.flush()

        # Set the session's primary thread pointer post-thread-creation.
        # This is the circular-FK shape the spec calls out: primary_thread_id
        # must be set after the root thread exists.
        session.primary_thread_id = thread.id
        db.flush()

        alias = SessionThreadAlias(
            thread_id=thread.id,
            provider="codex",
            alias_kind="provider_session_id",
            alias_value="codex-thread-abc",
        )
        db.add(alias)
        edge = SessionEdge(
            provider="codex",
            edge_kind="unknown",
            visibility="timeline",
            evidence_kind="test",
            target_session_id=session.id,
            target_thread_id=thread.id,
            provider_edge_id="parent:codex-thread-abc",
            metadata_json={"parent_provider_session_id": "parent"},
        )
        db.add(edge)

        run = SessionRun(
            thread_id=thread.id,
            provider="codex",
            host_id="dev-machine",
            pid=12345,
            process_start_time=now,
            cwd="/tmp",
            launch_origin="longhouse_spawned",
            started_at=now,
        )
        db.add(run)
        db.flush()

        conn = SessionConnection(
            run_id=run.id,
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            can_send_input=1,
            can_interrupt=1,
            can_terminate=1,
            can_tail_output=1,
            can_resume=1,
        )
        db.add(conn)

        attempt = SessionLaunchAttempt(
            session_id=session.id,
            thread_id=thread.id,
            run_id=run.id,
            provider="codex",
            host_id="dev-machine",
            client_request_id="cr-1",
            command_id="cmd-1",
            state="dispatched",
        )
        db.add(attempt)

        event = AgentEvent(
            session_id=session.id,
            thread_id=thread.id,
            role="user",
            content_text="hello",
            timestamp=now,
        )
        db.add(event)

        db.commit()

        # Reload and verify the graph.
        loaded_session = db.query(AgentSession).first()
        assert loaded_session.primary_thread_id == thread.id
        loaded_thread = db.query(SessionThread).first()
        assert loaded_thread.session_id == loaded_session.id
        assert loaded_thread.is_primary == 1
        assert db.query(SessionThreadAlias).count() == 1
        loaded_edge = db.query(SessionEdge).first()
        assert loaded_edge.target_thread_id == loaded_thread.id
        assert loaded_edge.edge_kind == "unknown"
        assert loaded_edge.metadata_json["parent_provider_session_id"] == "parent"
        loaded_run = db.query(SessionRun).first()
        assert loaded_run.thread_id == loaded_thread.id
        loaded_conn = db.query(SessionConnection).first()
        assert loaded_conn.run_id == loaded_run.id
        assert loaded_conn.state == "attached"
        assert loaded_conn.can_send_input == 1
        loaded_attempt = db.query(SessionLaunchAttempt).first()
        assert loaded_attempt.client_request_id == "cr-1"
        loaded_event = db.query(AgentEvent).first()
        assert loaded_event.thread_id == loaded_thread.id


def test_alias_lookup_index(tmp_path):
    """Aliases are evidence; resolver looks them up by (provider, alias_kind,
    alias_value). Confirm the index exists so Phase 2/4 lookups stay cheap.
    """
    engine = _engine(tmp_path)
    insp = inspect(engine)
    indexes = {idx["name"] for idx in insp.get_indexes("session_thread_aliases")}
    assert "ix_thread_aliases_lookup" in indexes


def test_edge_lookup_indexes(tmp_path):
    engine = _engine(tmp_path)
    insp = inspect(engine)
    indexes = {idx["name"] for idx in insp.get_indexes("session_edges")}
    assert "ix_session_edges_source" in indexes
    assert "ix_session_edges_target" in indexes
    assert "ix_session_edges_provider_edge" in indexes


def test_launch_attempt_idempotency_index(tmp_path):
    """`(session_id, client_request_id)` must be unique-where-not-null so
    retries don't double-dispatch.
    """
    engine = _engine(tmp_path)
    insp = inspect(engine)
    indexes = insp.get_indexes("session_launch_attempts")
    by_name = {idx["name"]: idx for idx in indexes}
    idx = by_name.get("ix_launch_attempts_session_client_request")
    assert idx is not None, "idempotency index missing"
    assert bool(idx.get("unique"))

    # Verify the partial predicate actually landed in SQLite. SQLAlchemy's
    # inspector exposes it via dialect_options on SQLite, but the canonical
    # check is the persisted index SQL.
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ix_launch_attempts_session_client_request'"
        ).fetchall()
    assert rows, "idempotency index not persisted"
    sql = (rows[0][0] or "").lower()
    assert "where" in sql and "client_request_id is not null" in sql, sql


def test_one_primary_thread_per_session_enforced(tmp_path):
    """Two `is_primary=1` rows for the same session must be impossible.

    The unique partial index protects backfill / Phase 2 from silently
    creating a second primary thread on a race or bug.
    """
    from sqlalchemy.exc import IntegrityError

    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        s = AgentSession(
            provider="codex",
            environment="test",
            project="zerg",
            device_id="dev",
            started_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        )
        db.add(s)
        db.flush()
        db.add(SessionThread(session_id=s.id, provider="codex", branch_kind="root", is_primary=1))
        db.flush()

        # Second primary on same session — must raise.
        db.add(SessionThread(session_id=s.id, provider="codex", branch_kind="root", is_primary=1))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("expected IntegrityError on second primary thread")

    # Non-primary children are fine: any number of subagent threads may exist.
    with SessionLocal() as db:
        s = AgentSession(
            provider="codex",
            environment="test",
            project="zerg",
            device_id="dev",
            started_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        )
        db.add(s)
        db.flush()
        db.add(SessionThread(session_id=s.id, provider="codex", branch_kind="root", is_primary=1))
        db.add(SessionThread(session_id=s.id, provider="codex", branch_kind="subagent", is_primary=0))
        db.add(SessionThread(session_id=s.id, provider="codex", branch_kind="subagent", is_primary=0))
        db.flush()


def test_thread_alias_unique_per_thread_enforced(tmp_path):
    """Same alias tuple on the same thread must not duplicate.

    Globally the same alias may appear on multiple threads (e.g. copied
    transcripts pre-divergence) — the constraint scopes to thread.
    """
    from sqlalchemy.exc import IntegrityError

    engine = _engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        s = AgentSession(
            provider="codex",
            environment="test",
            project="zerg",
            device_id="dev",
            started_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        )
        db.add(s)
        db.flush()
        t1 = SessionThread(session_id=s.id, provider="codex", branch_kind="root", is_primary=1)
        db.add(t1)
        db.flush()

        db.add(
            SessionThreadAlias(
                thread_id=t1.id,
                provider="codex",
                alias_kind="provider_session_id",
                alias_value="codex-1",
            )
        )
        db.flush()

        db.add(
            SessionThreadAlias(
                thread_id=t1.id,
                provider="codex",
                alias_kind="provider_session_id",
                alias_value="codex-1",
            )
        )
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("expected IntegrityError on duplicate alias")

    # Same alias on a *different* thread is allowed (evidence, not identity).
    with SessionLocal() as db:
        s = AgentSession(
            provider="codex",
            environment="test",
            project="zerg",
            device_id="dev",
            started_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        )
        db.add(s)
        db.flush()
        t1 = SessionThread(session_id=s.id, provider="codex", branch_kind="root", is_primary=1)
        t2 = SessionThread(session_id=s.id, provider="codex", branch_kind="subagent", is_primary=0)
        db.add(t1)
        db.add(t2)
        db.flush()
        db.add(
            SessionThreadAlias(
                thread_id=t1.id,
                provider="codex",
                alias_kind="provider_session_id",
                alias_value="shared-codex",
            )
        )
        db.add(
            SessionThreadAlias(
                thread_id=t2.id,
                provider="codex",
                alias_kind="provider_session_id",
                alias_value="shared-codex",
            )
        )
        db.flush()
