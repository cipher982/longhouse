from datetime import datetime
from datetime import timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest


def test_agents_search_fts_sqlite(tmp_path):
    db_path = tmp_path / "fts.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="fts",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello fts search",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    ),
                    EventIngest(
                        role="tool",
                        content_text=None,
                        tool_name="Bash",
                        tool_output_text="grep output sample",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=1,
                    ),
                ],
            )
        )

        # Ensure FTS index is populated via triggers
        count = db.execute(text("SELECT count(*) FROM events_fts")).scalar()
        assert count == 2

        # Search via FTS table directly
        rows = db.execute(
            text("SELECT DISTINCT session_id FROM events_fts WHERE events_fts MATCH :query"),
            {"query": "hello"},
        ).fetchall()
        assert rows

        # Search via store API (should return the session)
        sessions, total = store.list_sessions(include_test=True, query="grep")
        assert total == 1
        assert len(sessions) == 1

        matches = store.get_session_matches([sessions[0].id], "grep")
        assert sessions[0].id in matches
        assert matches[sessions[0].id]["event_id"]
        assert "grep" in (matches[sessions[0].id]["snippet"] or "").lower()


def test_agents_fts_triggers_update_delete(tmp_path):
    db_path = tmp_path / "fts_triggers.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="fts",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="needleone original content",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="needletwo second content",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=1,
                    ),
                ],
            )
        )

        rows = db.execute(text("SELECT id FROM events ORDER BY id")).fetchall()
        assert len(rows) == 2
        first_id = rows[0][0]
        second_id = rows[1][0]

        # Update content and ensure old term is removed from FTS index.
        db.execute(
            text("UPDATE events SET content_text = :text WHERE id = :id"),
            {"text": "needleupdated new content", "id": first_id},
        )
        db.commit()

        count_old = db.execute(
            text("SELECT count(*) FROM events_fts WHERE events_fts MATCH :query"),
            {"query": "needleone"},
        ).scalar()
        assert count_old == 0

        count_new = db.execute(
            text("SELECT count(*) FROM events_fts WHERE events_fts MATCH :query"),
            {"query": "needleupdated"},
        ).scalar()
        assert count_new == 1

        # Delete event and ensure term is removed from FTS index.
        db.execute(text("DELETE FROM events WHERE id = :id"), {"id": second_id})
        db.commit()

        count_deleted = db.execute(
            text("SELECT count(*) FROM events_fts WHERE events_fts MATCH :query"),
            {"query": "needletwo"},
        ).scalar()
        assert count_deleted == 0


def test_agents_fts_small_append_keeps_triggers_enabled(tmp_path):
    db_path = tmp_path / "fts_small_append.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        initial = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="fts",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="seed event one",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="seed event two",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=1,
                    ),
                ],
            )
        )

        def fail_disable_triggers():
            raise AssertionError("small transcript appends should not disable FTS triggers")

        store._disable_fts_triggers = fail_disable_triggers
        append_events = [
            EventIngest(
                role="assistant" if index % 2 else "user",
                content_text=f"small append event {index}",
                timestamp=datetime(2026, 2, 5, 0, 0, index + 2, tzinfo=timezone.utc),
                source_path="/tmp/session.jsonl",
                source_offset=index + 2,
            )
            for index in range(12)
        ]
        store.ingest_session(
            SessionIngest(
                id=initial.session_id,
                provider="claude",
                environment="test",
                project="fts",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                events=append_events,
            )
        )

        count = db.execute(text("SELECT count(*) FROM events_fts")).scalar()
        assert count == 14


def test_agents_fts_large_append_backfills_inserted_rows_only(tmp_path):
    db_path = tmp_path / "fts_large_append.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        initial = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="fts",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="seed event one",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="seed event two",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=1,
                    ),
                ],
            )
        )

        captured_ids: list[int] = []
        original_backfill = store._backfill_fts_for_event_ids

        def capture_event_backfill(event_ids):
            captured_ids.extend(event_ids)
            return original_backfill(event_ids)

        def fail_session_backfill(_session_id):
            raise AssertionError("large transcript appends should backfill only the inserted rows")

        store._backfill_fts_for_event_ids = capture_event_backfill
        store._backfill_fts_for_session = fail_session_backfill

        append_events = [
            EventIngest(
                role="assistant" if index % 2 else "user",
                content_text=f"large append event {index}",
                timestamp=datetime(2026, 2, 5, 0, 1, index % 60, tzinfo=timezone.utc),
                source_path="/tmp/session.jsonl",
                source_offset=index + 2,
            )
            for index in range(120)
        ]
        result = store.ingest_session(
            SessionIngest(
                id=initial.session_id,
                provider="claude",
                environment="test",
                project="fts",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                events=append_events,
            )
        )

        assert result.events_inserted == 120
        assert len(captured_ids) == 120
        assert len(set(captured_ids)) == 120

        count = db.execute(text("SELECT count(*) FROM events_fts")).scalar()
        assert count == 122


def test_agents_fts_large_append_restores_triggers_after_error(tmp_path):
    db_path = tmp_path / "fts_restore_after_error.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        initial = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="fts",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="seed event one",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="seed event two",
                        timestamp=datetime(2026, 2, 5, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=1,
                    ),
                ],
            )
        )

        original_compute_event_hash = store._compute_event_hash
        hash_calls = 0

        def fail_after_committed_chunk(event):
            nonlocal hash_calls
            hash_calls += 1
            if hash_calls > 205:
                raise RuntimeError("boom after committed chunk")
            return original_compute_event_hash(event)

        store._compute_event_hash = fail_after_committed_chunk
        append_events = [
            EventIngest(
                role="assistant" if index % 2 else "user",
                content_text=f"large append event {index}",
                timestamp=datetime(2026, 2, 5, 0, 2, index % 60, tzinfo=timezone.utc),
                source_path="/tmp/session.jsonl",
                source_offset=index + 2,
            )
            for index in range(220)
        ]

        with pytest.raises(RuntimeError, match="boom after committed chunk"):
            store.ingest_session(
                SessionIngest(
                    id=initial.session_id,
                    provider="claude",
                    environment="test",
                    project="fts",
                    device_id="dev-machine",
                    cwd="/tmp",
                    git_repo=None,
                    git_branch=None,
                    started_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
                    events=append_events,
                )
            )

        db.rollback()

        trigger_names = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name IN ('events_ai', 'events_ad', 'events_au')"
                )
            ).fetchall()
        }
        assert trigger_names == {"events_ai", "events_ad", "events_au"}

        events_count = db.execute(text("SELECT count(*) FROM events")).scalar()
        fts_count = db.execute(text("SELECT count(*) FROM events_fts")).scalar()
        assert events_count == 202
        assert fts_count == 202
