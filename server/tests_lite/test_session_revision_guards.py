"""Tests for replay-safe transcript revision guards on summary/embed work."""

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase


def _make_db(tmp_path, name: str) -> make_sessionmaker:
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


@pytest.mark.asyncio
async def test_generate_summary_impl_skips_provider_when_summary_revision_current(tmp_path):
    from zerg.services.session_summaries import generate_summary_impl

    factory = _make_db(tmp_path, "summary_revision_current.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        summary="Already summarized",
        summary_title="Current summary",
        transcript_revision=3,
        summary_revision=3,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    db.close()

    settings = SimpleNamespace(testing=False, llm_disabled=False)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_with_db_fallback",
            side_effect=AssertionError("summary provider should not be fetched when summary revision is current"),
        ),
    ):
        await generate_summary_impl(str(session.id))


@pytest.mark.asyncio
async def test_generate_embeddings_impl_skips_provider_when_embedding_revision_current(tmp_path):
    from zerg.services.session_summaries import generate_embeddings_impl

    factory = _make_db(tmp_path, "embedding_revision_current.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        needs_embedding=1,
        transcript_revision=4,
        embedding_revision=4,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    db.close()

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch(
            "zerg.models_config.get_embedding_config_with_db_fallback",
            side_effect=AssertionError("embedding config should not be loaded when embedding revision is current"),
        ),
    ):
        await generate_embeddings_impl(str(session.id))


@pytest.mark.asyncio
async def test_summarize_and_persist_updates_summary_revision(tmp_path, monkeypatch):
    from zerg.services.session_summaries import summarize_and_persist

    factory = _make_db(tmp_path, "summary_revision_persist.db")

    async def _fake_summarize_events(_events, *, client, model, metadata):
        return SimpleNamespace(summary="Fixed the login flow", title="Login flow")

    monkeypatch.setattr("zerg.services.session_processing.summarize_events", _fake_summarize_events)

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=2,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text="Please fix login",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.add(
        AgentEvent(
            session_id=session.id,
            role="assistant",
            content_text="I fixed login",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()

    events = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).order_by(AgentEvent.id).all()

    summary = await summarize_and_persist(session, events, db, client=object(), model="test-model")
    assert summary is not None

    db.expire_all()
    refreshed = db.query(AgentSession).filter(AgentSession.id == session.id).one()
    db.close()

    assert refreshed.summary == "Fixed the login flow"
    assert refreshed.summary_title == "Login flow"
    assert refreshed.summary_revision == 2
