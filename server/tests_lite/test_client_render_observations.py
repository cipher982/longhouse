"""Shared client-render observation reader."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.services.client_render_observations import list_client_render_observations
from zerg.services.session_observations import OBS_KIND_CLIENT_RENDER
from zerg.services.session_observations import SOURCE_DOMAIN_CLIENT
from zerg.services.session_observations import record_session_observation

PINNED_NOW = datetime(2026, 5, 22, 18, 0, 0, tzinfo=timezone.utc)


def _make_db(tmp_path):
    db_path = tmp_path / "test_client_render_observations.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_session(db) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="zerg",
        device_id="cinder",
        provider_session_id=str(uuid4()),
        started_at=PINNED_NOW,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _record_render(db, *, session_id, event_id: str, surface: str, managed: object) -> None:
    record_session_observation(
        db,
        observation_id=f"client_render:{surface}:{session_id}:{event_id}",
        session_id=session_id,
        runtime_key=None,
        provider="codex",
        device_id="cinder",
        source_domain=SOURCE_DOMAIN_CLIENT,
        source="client_render_beacon",
        kind=OBS_KIND_CLIENT_RENDER,
        source_cursor=f"event:{event_id}",
        observed_at=PINNED_NOW,
        payload={
            "event_id": event_id,
            "surface": surface,
            "managed": managed,
            "latency_ms": "101.5",
            "webkit": {"render_duration_ms": "42"},
        },
    )
    db.commit()


def test_list_client_render_observations_normalizes_payload_filters(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        session_id = session.id
        _record_render(db, session_id=session_id, event_id="ios", surface="IOS", managed="true")
        _record_render(db, session_id=session_id, event_id="web", surface="web", managed=False)

        result = list_client_render_observations(
            db,
            session_id=session_id,
            provider="CODEX",
            surface="ios",
            managed=True,
        )

    assert result.truncated is False
    assert len(result.rows) == 1
    observation = result.rows[0]
    assert observation.session_id == str(session_id)
    assert observation.provider == "codex"
    assert observation.surface == "ios"
    assert observation.managed is True
    assert observation.latency_ms == 101
    assert observation.ios_render_duration_ms == 42
    assert observation.payload["event_id"] == "ios"


def test_list_client_render_observations_reports_truncation(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        session_id = session.id
        _record_render(db, session_id=session_id, event_id="first", surface="web", managed=True)
        _record_render(db, session_id=session_id, event_id="second", surface="web", managed=True)

        result = list_client_render_observations(db, session_id=session_id, limit=1)

    assert result.truncated is True
    assert len(result.rows) == 1
