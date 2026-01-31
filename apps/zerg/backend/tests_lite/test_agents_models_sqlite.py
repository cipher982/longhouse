from datetime import datetime
from datetime import timezone

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase


def test_agents_models_roundtrip_sqlite(tmp_path):
    db_path = tmp_path / "agents.db"
    engine = make_engine(f"sqlite:///{db_path}")
    # Strip schema for SQLite (models use schema="agents" for Postgres)
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        session = AgentSession(
            provider="codex",
            environment="test",
            project="zerg",
            device_id="dev-machine",
            cwd="/tmp",
            git_repo=None,
            git_branch=None,
            started_at=datetime(2026, 1, 31, tzinfo=timezone.utc),
            ended_at=None,
            provider_session_id="session-1",
        )
        db.add(session)
        db.flush()

        event = AgentEvent(
            session_id=session.id,
            role="user",
            content_text="hello",
            tool_name=None,
            tool_input_json=None,
            tool_output_text=None,
            timestamp=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )
        db.add(event)
        db.commit()

        loaded = db.query(AgentSession).first()
        assert loaded is not None
        assert loaded.events
