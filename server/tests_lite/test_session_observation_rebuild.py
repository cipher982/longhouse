"""Safety and replay coverage for the still-active observation rebuild service."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.services.session_observation_rebuild import SessionObservationRebuildCoverageError
from zerg.services.session_observation_rebuild import rebuild_session_observation_projections
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events


def _sessionmaker(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'observation_rebuild.db'}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_session(db, *, started_at: datetime) -> AgentSession:
    session = AgentSession(
        provider="codex",
        environment="test",
        project="observation-rebuild",
        device_id="cinder",
        started_at=started_at,
        last_activity_at=started_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_rebuild_refuses_to_delete_uncovered_transcript_rows(tmp_path):
    factory = _sessionmaker(tmp_path)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    with factory() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))
        db.add(AgentEvent(session_id=session.id, role="assistant", content_text="keep me", timestamp=now))
        db.commit()

        with pytest.raises(SessionObservationRebuildCoverageError, match="no transcript observations"):
            rebuild_session_observation_projections(db, session_id=session.id)

        assert db.query(AgentEvent.content_text).filter(AgentEvent.session_id == session.id).scalar() == "keep me"


def test_runtime_observation_rebuild_is_idempotent(tmp_path):
    factory = _sessionmaker(tmp_path)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    with factory() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))
        runtime_key = f"codex:{session.id}"
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="running",
                    tool_name="Bash",
                    occurred_at=now,
                    dedupe_key=f"phase:{session.id}:1",
                    payload={"managed_transport": "codex_app_server"},
                )
            ],
        )
        db.commit()

        first = rebuild_session_observation_projections(db, session_id=session.id, runtime_key=runtime_key)
        second = rebuild_session_observation_projections(db, session_id=session.id, runtime_key=runtime_key)
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()

    assert first.runtime_signals_reduced == second.runtime_signals_reduced == 1
    assert state.phase == "running"
    assert state.active_tool == "Bash"
