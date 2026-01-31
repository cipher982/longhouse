"""Regression test for SQLite duplicate handling.

Tests that duplicate event insertion doesn't leave the SQLAlchemy session
in a failed state (PendingRollbackError).
"""

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


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
    AgentsBase.metadata.create_all(bind=engine)

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

        # 5. Verify final state - 2 events total (1 original + 1 new, duplicate skipped)
        events = store.get_session_events(session_id)
        assert len(events) == 2


def test_duplicate_event_different_hash(tmp_path):
    """Test that events with same source_path/offset but different content are not duplicates.

    The unique constraint includes event_hash, so different content = different event.
    """
    db_path = tmp_path / "duplicate_hash.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)

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

        # Should insert because hash is different
        assert result2.events_inserted == 1
        assert result2.events_skipped == 0

        events = store.get_session_events(session_id)
        assert len(events) == 2
