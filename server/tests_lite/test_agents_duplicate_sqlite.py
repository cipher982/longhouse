"""Regression test for SQLite duplicate handling.

Tests that duplicate event insertion doesn't leave the SQLAlchemy session
in a failed state (PendingRollbackError).
"""

from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest


def test_duplicate_event_sqlite_no_pending_rollback(tmp_path):
    """Test that duplicate events are handled without leaving session in failed state.

    Regression test for: SQLite duplicate handling leaves session in failed state.
    The fix uses on_conflict_do_nothing() instead of try/except which would leave
    the session needing rollback.
    """
    db_path = tmp_path / "duplicate.db"
    engine = make_engine(f"sqlite:///{db_path}")
    # Strip schema for SQLite (models use schema="agents" for Postgres)
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)

        # Create a session with source_path set
        base_time = datetime(2026, 1, 31, 12, 0, 0, tzinfo=timezone.utc)

        # 1. Insert first event
        result1 = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="test-duplicate",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=base_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello world",
                        timestamp=base_time,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        session_id = result1.session_id
        assert result1.events_inserted == 1
        assert result1.events_skipped == 0

        # 2. Attempt to insert the same event again (duplicate)
        # Before the fix, this would leave the session in failed state
        result2 = store.ingest_session(
            SessionIngest(
                id=session_id,  # Same session
                provider="codex",
                environment="test",
                project="test-duplicate",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=base_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello world",  # Same content
                        timestamp=base_time,  # Same timestamp
                        source_path="/tmp/session.jsonl",  # Same source_path
                        source_offset=0,  # Same offset
                    )
                ],
            )
        )

        # 3. Verify duplicate was skipped correctly
        assert result2.events_inserted == 0
        assert result2.events_skipped == 1

        # 4. Verify session can still insert more events after duplicate
        # This is the key test - before the fix, this would raise PendingRollbackError
        result3 = store.ingest_session(
            SessionIngest(
                id=session_id,  # Same session
                provider="codex",
                environment="test",
                project="test-duplicate",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=base_time,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="Hello! How can I help?",
                        timestamp=datetime(2026, 1, 31, 12, 0, 1, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=100,  # Different offset
                    )
                ],
            )
        )

        assert result3.events_inserted == 1
        assert result3.events_skipped == 0

        # 5. Verify final state on active head branch.
        events = store.get_session_events(session_id, branch_mode="head")
        assert len(events) == 2


def test_duplicate_event_different_hash(tmp_path):
    """Test that events with same source_path/offset but different content are not duplicates.

    The unique constraint includes event_hash, so different content = different event.
    """
    db_path = tmp_path / "duplicate_hash.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)

        base_time = datetime(2026, 1, 31, 12, 0, 0, tzinfo=timezone.utc)

        # Insert first event
        result1 = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="test-hash",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=base_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="version 1",
                        timestamp=base_time,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        session_id = result1.session_id
        assert result1.events_inserted == 1

        # Insert event with same source_path/offset but different content
        # This should be treated as a new event due to different hash
        result2 = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="test",
                project="test-hash",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=base_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="version 2",  # Different content = different hash
                        timestamp=base_time,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,  # Same offset
                    )
                ],
            )
        )

        # Should insert because hash is different.
        assert result2.events_inserted == 1
        assert result2.events_skipped == 0

        # Without source-line data, ingest cannot detect rewind semantics.
        head_events = store.get_session_events(session_id, branch_mode="head")
        assert len(head_events) == 2

        all_events = store.get_session_events(session_id, branch_mode="all")
        assert len(all_events) == 2


def test_duplicate_ingest_upgrades_generic_environment_to_machine_label(tmp_path):
    db_path = tmp_path / "duplicate_metadata_upgrade.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)

        base_time = datetime(2026, 3, 8, 16, 38, 52, tzinfo=timezone.utc)
        later_time = datetime(2026, 3, 8, 16, 39, 0, tzinfo=timezone.utc)

        first = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="production",
                device_id="host-123",
                started_at=base_time,
                ended_at=base_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="please review",
                        timestamp=base_time,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        second = store.ingest_session(
            SessionIngest(
                id=first.session_id,
                provider="claude",
                environment="work-laptop",
                project="sample-project",
                device_id="host-123",
                cwd="/workspace/sample-project",
                git_repo="git@github.com:example/sample-project.git",
                git_branch="main",
                started_at=base_time,
                ended_at=later_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="please review",
                        timestamp=base_time,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        assert second.events_inserted == 0
        assert second.events_skipped == 1

        stored = db.query(AgentSession).filter(AgentSession.id == first.session_id).one()
        assert stored.environment == "work-laptop"
        assert stored.project == "sample-project"
        assert stored.cwd == "/workspace/sample-project"
        assert stored.git_repo == "git@github.com:example/sample-project.git"
        assert stored.git_branch == "main"
        # Phase 4 of session-liveness-honesty: ingest-supplied `ended_at`
        # is routed into last_activity_at, not session.ended_at. Only an
        # explicit terminal_signal (or Phase 6 process-gone) sets ended_at.
        assert stored.ended_at is None
        assert stored.last_activity_at == later_time.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("provider", "managed_transport"),
    [
        ("codex", "codex_app_server"),
        ("antigravity", "antigravity_hook_inbox"),
    ],
)
def test_duplicate_ingest_replaces_managed_local_placeholder_provider_session_id(
    tmp_path,
    provider,
    managed_transport,
):
    db_path = tmp_path / f"duplicate_{provider}_placeholder_upgrade.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)

        base_time = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)
        session_id = uuid4()
        native_provider_session_id = f"{provider}-native-session"

        launched = AgentSession(
            id=session_id,
            provider=provider,
            environment="development",
            project=f"managed-local-{provider}",
            device_id="cinder",
            cwd="/tmp/zerg",
            started_at=base_time,
            ended_at=base_time,
            provider_session_id=str(session_id),
            thread_root_session_id=session_id,
            continuation_kind="local",
            origin_label="cinder",
            execution_home="managed_local",
            managed_transport=managed_transport,
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            is_writable_head=1,
            is_sidechain=0,
        )
        db.add(launched)
        db.commit()

        first = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider=provider,
                environment="development",
                project=f"managed-local-{provider}",
                device_id="cinder",
                cwd="/tmp/zerg",
                started_at=base_time,
                provider_session_id=str(session_id),
                events=[
                    EventIngest(
                        role="user",
                        content_text="continue",
                        timestamp=base_time,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        assert first.events_inserted == 1
        assert first.events_skipped == 0

        second = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider=provider,
                environment="development",
                project=f"managed-local-{provider}",
                device_id="cinder",
                cwd="/tmp/zerg",
                started_at=base_time,
                provider_session_id=native_provider_session_id,
                events=[
                    EventIngest(
                        role="user",
                        content_text="continue",
                        timestamp=base_time,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        assert second.events_inserted == 0
        assert second.events_skipped == 1
        assert db.query(AgentSession).count() == 1

        stored = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        # Session-identity-kernel cleanup: ``provider_session_id`` is no
        # longer a column. The native provider session id is recorded as a
        # ``session_thread_aliases`` row scoped to the primary thread.
        from zerg.models.agents import SessionThread, SessionThreadAlias

        alias_values = {
            row[0]
            for row in db.query(SessionThreadAlias.alias_value)
            .join(SessionThread, SessionThreadAlias.thread_id == SessionThread.id)
            .filter(SessionThread.session_id == stored.id)
            .filter(SessionThreadAlias.alias_kind == "provider_session_id")
            .all()
        }
        assert native_provider_session_id in alias_values


def test_duplicate_ingest_keeps_machine_label_when_generic_environment_arrives_later(tmp_path):
    db_path = tmp_path / "duplicate_metadata_preserve.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)

        base_time = datetime(2026, 3, 8, 16, 59, 38, tzinfo=timezone.utc)

        first = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="work-laptop",
                project="sample-project",
                device_id="host-123",
                cwd="/workspace/sample-project",
                started_at=base_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="fix this correctly",
                        timestamp=base_time,
                        source_path="/tmp/codex.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        second = store.ingest_session(
            SessionIngest(
                id=first.session_id,
                provider="codex",
                environment="production",
                project="sample-project",
                device_id="host-123",
                cwd="/workspace/sample-project",
                started_at=base_time,
                events=[
                    EventIngest(
                        role="user",
                        content_text="fix this correctly",
                        timestamp=base_time,
                        source_path="/tmp/codex.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        assert second.events_inserted == 0
        assert second.events_skipped == 1

        stored = db.query(AgentSession).filter(AgentSession.id == first.session_id).one()
        assert stored.environment == "work-laptop"


def test_duplicate_replay_without_source_line_delta_does_not_requeue_post_ingest_work(tmp_path):
    db_path = tmp_path / "duplicate_replay_no_source_delta.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)

        started_at = datetime(2026, 4, 14, 21, 46, 0, tzinfo=timezone.utc)
        naive_event_time = datetime(2026, 1, 31, 12, 0, 0)
        aware_event_time = datetime(2026, 1, 31, 12, 0, 0, tzinfo=timezone.utc)
        source_path = "/tmp/codex-session.jsonl"
        raw_line = (
            '{"type":"response_item","timestamp":"2026-01-31T12:00:00Z",'
            '"payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello world"}]}}'
        )

        first = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="duplicate-replay",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello world",
                        timestamp=naive_event_time,
                        source_path=source_path,
                        source_offset=0,
                        raw_json=raw_line,
                    )
                ],
                source_lines=[
                    SourceLineIngest(
                        source_path=source_path,
                        source_offset=0,
                        raw_json=raw_line,
                    )
                ],
            )
        )

        session_id = first.session_id
        assert first.events_inserted == 1
        assert db.query(SessionTask).filter(SessionTask.session_id == str(session_id)).count() == 0
        initial_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert initial_session.transcript_revision == 1
        assert initial_session.needs_embedding == 1

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.needs_embedding = 0
        db.commit()

        second = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="test",
                project="duplicate-replay",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello world",
                        timestamp=aware_event_time,
                        source_path=source_path,
                        source_offset=0,
                        raw_json=raw_line,
                    )
                ],
                source_lines=[
                    SourceLineIngest(
                        source_path=source_path,
                        source_offset=0,
                        raw_json=raw_line,
                    )
                ],
            )
        )

        assert second.events_inserted == 0
        assert second.events_skipped == 1

        stored = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert stored.needs_embedding == 0
        assert stored.transcript_revision == 1
        assert stored.user_messages == 1
        assert stored.assistant_messages == 0
        assert stored.tool_calls == 0
        assert store.count_session_events(session_id, branch_mode="head") == 1
        assert db.query(SessionTask).filter(SessionTask.session_id == str(session_id)).count() == 0


def test_branch_source_line_lookup_can_limit_to_incoming_offsets(tmp_path):
    db_path = tmp_path / "source_line_lookup_offsets.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        started_at = datetime(2026, 4, 14, 21, 46, 0, tzinfo=timezone.utc)
        source_path = "/tmp/codex-session.jsonl"

        result = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="source-line-lookup",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=started_at,
                source_lines=[
                    SourceLineIngest(
                        source_path=source_path,
                        source_offset=offset,
                        raw_json=f'{{"offset":{offset}}}',
                    )
                    for offset in range(100)
                ],
            )
        )

        head_branch_id = store.get_head_branch_id(result.session_id)
        latest_by_offset, max_offset_by_path = store._list_branch_source_lines(
            result.session_id,
            head_branch_id,
            {source_path},
            source_offsets_by_path={source_path: {10, 90}},
        )

        assert set(latest_by_offset) == {(source_path, 10), (source_path, 90)}
        assert max_offset_by_path == {source_path: 99}

        latest_without_max, max_without_max = store._list_branch_source_lines(
            result.session_id,
            head_branch_id,
            {source_path},
            source_offsets_by_path={source_path: {10, 90}},
            include_max_offsets=False,
        )

        assert set(latest_without_max) == {(source_path, 10), (source_path, 90)}
        assert max_without_max == {}
