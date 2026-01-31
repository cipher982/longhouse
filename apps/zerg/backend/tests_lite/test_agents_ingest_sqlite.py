from datetime import datetime
from datetime import timezone

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


@pytest.mark.xfail(
    reason="AgentsStore uses PostgreSQL insert; Phase 3 will add SQLite upsert",
    strict=True,
)
def test_agents_ingest_sqlite(tmp_path):
    db_path = tmp_path / "ingest.db"
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 1, 31, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello",
                        timestamp=datetime(2026, 1, 31, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.events_inserted == 1
        assert result.events_skipped == 0
