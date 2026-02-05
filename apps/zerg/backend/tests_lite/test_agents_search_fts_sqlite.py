from datetime import datetime
from datetime import timezone

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


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
