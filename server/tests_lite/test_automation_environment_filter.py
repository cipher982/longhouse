"""Regression test: automation sessions are filterable by environment.

Verifies that sessions ingested with environment=automation metadata are
correctly returned (or excluded) by the AgentsStore.list_sessions filter.
"""

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.database import Base
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest


def test_environment_filter_returns_automation_sessions(tmp_path):
    """Ingest an automation session and verify the environment filter works."""
    db_path = tmp_path / "env_filter.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    Session = sessionmaker(bind=engine)
    with Session() as db:
        store = AgentsStore(db)

        # Ingest a session with environment=automation
        store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="automation",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="automation completed task",
                        timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        # Filter by environment=automation should return the session
        sessions, total = store.list_sessions(environment="automation", hide_autonomous=False)
        assert total == 1
        assert sessions[0].environment == "automation"

        # Filter by environment=production should NOT return it
        sessions, total = store.list_sessions(environment="production", hide_autonomous=False)
        assert total == 0
