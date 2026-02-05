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
