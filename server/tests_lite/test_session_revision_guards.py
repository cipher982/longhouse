"""Tests for replay-safe transcript revision guards on summary/embed work."""

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import numpy as np
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession


def _make_db(tmp_path, name: str) -> make_sessionmaker:
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
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
            "zerg.models_config.get_llm_client_for_use_case",
            side_effect=AssertionError("summary provider should not be fetched when summary revision is current"),
        ),
    ):
        await generate_summary_impl(str(session.id))


@pytest.mark.asyncio
async def test_generate_summary_impl_marks_summary_current_when_llm_disabled(tmp_path):
    from zerg.services.session_enrichment_reconciler import select_stale_summary_session_ids
    from zerg.services.session_summaries import generate_summary_impl

    factory = _make_db(tmp_path, "summary_llm_disabled.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=2,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    settings = SimpleNamespace(testing=False, llm_disabled=True)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            side_effect=AssertionError("summary provider should not be fetched when LLMs are disabled"),
        ),
    ):
        await generate_summary_impl(session_id)

    verify = factory()
    try:
        refreshed = verify.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed.summary_revision == 2
        assert refreshed.summary_title == "zerg"
        assert select_stale_summary_session_ids(verify, limit=10) == []
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_generate_summary_impl_marks_summary_current_when_llm_misconfigured(tmp_path):
    from zerg.services.session_enrichment_reconciler import select_stale_summary_session_ids
    from zerg.services.session_summaries import generate_summary_impl

    factory = _make_db(tmp_path, "summary_llm_misconfigured.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=2,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text="Please investigate hosted ingest timeouts.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.add(
        AgentEvent(
            session_id=session.id,
            role="assistant",
            content_text="The summary reconciler is repeatedly selecting sessions it cannot summarize.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.close()

    settings = SimpleNamespace(testing=False, llm_disabled=False)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            side_effect=ValueError("OPENROUTER_API_KEY required"),
        ),
    ):
        await generate_summary_impl(session_id)

    verify = factory()
    try:
        refreshed = verify.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed.summary_revision == 2
        assert refreshed.summary_title == "zerg"
        assert select_stale_summary_session_ids(verify, limit=10) == []
    finally:
        verify.close()


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
            "zerg.models_config.get_embedding_config",
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


@pytest.mark.asyncio
async def test_generate_summary_impl_releases_db_connection_during_llm_call(tmp_path, monkeypatch):
    from zerg.services.session_summaries import generate_summary_impl

    db_path = tmp_path / "summary_releases_connection.db"
    engine = make_engine(f"sqlite:///{db_path}", pool_size=1, max_overflow=0)
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

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
    session_id = str(session.id)
    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text="Please review the timeline session card state.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.add(
        AgentEvent(
            session_id=session.id,
            role="assistant",
            content_text="I found a summary worker holding database connections.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.close()

    observed_checked_out: list[int] = []

    async def _fake_incremental_summary(**_kwargs):
        observed_checked_out.append(engine.pool.checkedout())
        return SimpleNamespace(summary="Released the DB connection", title="DB connection release")

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr("zerg.services.session_processing.incremental_summary", _fake_incremental_summary)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "test-model", "test-provider"),
        ),
    ):
        await generate_summary_impl(session_id)

    assert observed_checked_out == [0]

    verify_db = factory()
    refreshed = verify_db.query(AgentSession).filter(AgentSession.id == session_id).one()
    verify_db.close()

    assert refreshed.summary == "Released the DB connection"
    assert refreshed.summary_title == "DB connection release"
    assert refreshed.summary_revision == 2
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_summary_impl_does_not_overwrite_with_placeholder_result(tmp_path, monkeypatch):
    from zerg.services.session_summaries import generate_summary_impl

    factory = _make_db(tmp_path, "summary_placeholder_result.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="test",
        project="floodmap",
        started_at=datetime.now(timezone.utc),
        summary="Verified the slider QA flow and aggregate runner.",
        summary_title="Slider QA Verified",
        transcript_revision=3,
        summary_revision=2,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text="Keep going on the CONUS audit.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.add(
        AgentEvent(
            session_id=session.id,
            role="assistant",
            content_text="I am running the next verifier pass.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.close()

    async def _fake_incremental_summary(**_kwargs):
        return SimpleNamespace(title="Untitled Session", summary="No summary generated.")

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr("zerg.services.session_processing.incremental_summary", _fake_incremental_summary)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "test-model", "test-provider"),
        ),
    ):
        await generate_summary_impl(session_id)

    verify_db = factory()
    refreshed = verify_db.query(AgentSession).filter(AgentSession.id == session_id).one()
    verify_db.close()

    assert refreshed.summary == "Verified the slider QA flow and aggregate runner."
    assert refreshed.summary_title == "Slider QA Verified"
    assert refreshed.summary_revision == 3
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_embeddings_impl_releases_db_connection_during_provider_call(tmp_path, monkeypatch):
    from zerg.models.agents import SessionEmbedding
    from zerg.services.session_summaries import generate_embeddings_impl

    db_path = tmp_path / "embedding_releases_connection.db"
    engine = make_engine(f"sqlite:///{db_path}", pool_size=1, max_overflow=0)
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        needs_embedding=1,
        transcript_revision=2,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text="Please diagnose health check flapping.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.add(
        AgentEvent(
            session_id=session.id,
            role="assistant",
            content_text="The embedding worker held database connections during provider calls.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.close()

    observed_checked_out: list[int] = []

    async def _fake_generate_embeddings(texts, _config):
        observed_checked_out.append(engine.pool.checkedout())
        return [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32) for _ in texts]

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")

    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embeddings", _fake_generate_embeddings)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.models_config.get_embedding_config", return_value=config),
    ):
        await generate_embeddings_impl(session_id)

    assert observed_checked_out == [0]

    verify_db = factory()
    stored = verify_db.query(SessionEmbedding).filter(SessionEmbedding.session_id == session_id).all()
    refreshed = verify_db.query(AgentSession).filter(AgentSession.id == session_id).one()
    verify_db.close()

    assert len(stored) == 2
    assert refreshed.needs_embedding == 0
    assert refreshed.embedding_revision == 2


@pytest.mark.asyncio
async def test_generate_embeddings_impl_raises_when_reconcile_makes_no_progress(tmp_path, monkeypatch):
    from zerg.services.session_summaries import generate_embeddings_impl

    factory = _make_db(tmp_path, "embedding_no_progress.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        needs_embedding=1,
        transcript_revision=3,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text="Force an embedding continuation with no writes.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.close()

    async def _fake_embed_session(*_args, **_kwargs):
        return 0, 1

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")
    monkeypatch.setattr("zerg.services.session_processing.embeddings.embed_session", _fake_embed_session)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.models_config.get_embedding_config", return_value=config),
    ):
        with pytest.raises(RuntimeError, match="made no progress"):
            await generate_embeddings_impl(session_id)

    verify_db = factory()
    refreshed = verify_db.query(AgentSession).filter(AgentSession.id == session_id).one()
    verify_db.close()

    assert refreshed.needs_embedding == 1
    assert refreshed.embedding_revision == 0
