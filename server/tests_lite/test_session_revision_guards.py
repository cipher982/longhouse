"""Tests for replay-safe transcript revision guards on summary/embed work."""

import asyncio
import os
from datetime import datetime
from datetime import timedelta
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
from zerg.models.agents import TimelineCard


def _make_db(tmp_path, name: str) -> make_sessionmaker:
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    from zerg.services.write_serializer import get_write_serializer

    get_write_serializer().configure(factory)
    return factory


@pytest.mark.asyncio
async def test_generate_summary_impl_skips_provider_when_summary_revision_current(tmp_path):
    from zerg.services.session_summaries import generate_summary_impl

    factory = _make_db(tmp_path, "summary_revision_current.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="cinder",
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
        environment="cinder",
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
        environment="cinder",
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
async def test_generate_summary_impl_skips_test_environment(tmp_path):
    from zerg.services.session_summaries import generate_summary_impl

    factory = _make_db(tmp_path, "summary_test_environment.db")

    db = factory()
    session = AgentSession(
        provider="opencode",
        environment="test",
        project="longhouse-provider-live-proof",
        started_at=datetime.now(timezone.utc),
        first_user_message_preview="LONGHOUSE_OPENCODE_NOREPLY_skip_summary",
        transcript_revision=3,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    settings = SimpleNamespace(testing=False, llm_disabled=False)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            side_effect=AssertionError("summary provider should not be fetched for test sessions"),
        ),
    ):
        await generate_summary_impl(session_id)

    verify = factory()
    try:
        refreshed = verify.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed.summary is None
        assert refreshed.summary_title is None
        assert refreshed.summary_revision == 0
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_generate_initial_title_impl_persists_stable_title(tmp_path, monkeypatch):
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session
    from zerg.services.session_summaries import generate_initial_title_impl

    reset_pubsub_for_test()
    factory = _make_db(tmp_path, "initial_title.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
        project="zerg",
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        first_user_message_preview="Can we make menu bar rows clearly clickable?",
        user_messages=1,
        assistant_messages=0,
        transcript_revision=1,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    captured: dict[str, object] = {}

    async def _fake_generate_initial_session_title(**kwargs):
        captured.update(kwargs)
        return "Menu Bar Row Affordance"

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr(
        "zerg.services.title_generator.generate_initial_session_title",
        _fake_generate_initial_session_title,
    )

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "deepseek/deepseek-v4-flash", "openrouter"),
        ) as get_client,
    ):
        updated = await generate_initial_title_impl(session_id)

    assert updated is True
    get_client.assert_called_once_with("session_title")
    client.close.assert_awaited_once()
    assert captured["first_user_message"] == "Can we make menu bar rows clearly clickable?"
    assert captured["model"] == "deepseek/deepseek-v4-flash"

    verify = factory()
    try:
        refreshed = verify.query(AgentSession).filter(AgentSession.id == session_id).one()
        card = verify.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        assert refreshed.summary_title == "Menu Bar Row Affordance"
        assert refreshed.anchor_title == "Menu Bar Row Affordance"
        assert refreshed.summary_revision == 1
        assert card.summary_title == "Menu Bar Row Affordance"
    finally:
        verify.close()

    bus = get_pubsub()
    with (
        bus.subscribe(topic_session(session_id), since_seq=0) as session_sub,
        bus.subscribe(TOPIC_TIMELINE, since_seq=0) as timeline_sub,
    ):
        session_msg = await session_sub.next_message(timeout=0.1)
        timeline_msg = await timeline_sub.next_message(timeout=0.1)

    assert session_msg is not None
    assert timeline_msg is not None
    for msg in (session_msg, timeline_msg):
        assert msg.payload["kind"] == "title_update"
        assert msg.payload["session_id"] == session_id
        assert msg.payload["provider"] == "codex"
        assert msg.payload["source"] == "initial_title"
    reset_pubsub_for_test()


@pytest.mark.asyncio
async def test_generate_initial_title_impl_does_not_publish_when_title_empty(tmp_path, monkeypatch):
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session
    from zerg.services.session_summaries import generate_initial_title_impl

    reset_pubsub_for_test()
    factory = _make_db(tmp_path, "initial_title_empty.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        first_user_message_preview="Please create an empty title regression test.",
        user_messages=1,
        assistant_messages=0,
        transcript_revision=1,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    async def _fake_generate_initial_session_title(**_kwargs):
        return '"""'

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr(
        "zerg.services.title_generator.generate_initial_session_title",
        _fake_generate_initial_session_title,
    )

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "deepseek/deepseek-v4-flash", "openrouter"),
        ),
    ):
        updated = await generate_initial_title_impl(session_id)

    assert updated is False
    assert get_pubsub().peek_latest_seq(topic_session(session_id)) == 0
    assert get_pubsub().peek_latest_seq(TOPIC_TIMELINE) == 0
    reset_pubsub_for_test()


@pytest.mark.asyncio
async def test_generate_initial_title_impl_times_out_optional_persist(tmp_path, monkeypatch):
    from zerg.services import session_summaries
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session

    reset_pubsub_for_test()
    factory = _make_db(tmp_path, "initial_title_timeout.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        first_user_message_preview="Please make optional title writes unable to wedge readiness.",
        user_messages=1,
        assistant_messages=0,
        transcript_revision=1,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    async def _fake_generate_initial_session_title(**_kwargs):
        return "Optional Title Write Timeout"

    async def _slow_to_thread(_fn):
        await asyncio.sleep(1)
        return 1

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr(
        "zerg.services.title_generator.generate_initial_session_title",
        _fake_generate_initial_session_title,
    )
    monkeypatch.setattr(session_summaries, "INITIAL_TITLE_WRITE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(session_summaries.asyncio, "to_thread", _slow_to_thread)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "deepseek/deepseek-v4-flash", "openrouter"),
        ),
    ):
        updated = await session_summaries.generate_initial_title_impl(session_id)

    assert updated is False
    client.close.assert_awaited_once()
    assert get_pubsub().peek_latest_seq(topic_session(session_id)) == 0
    assert get_pubsub().peek_latest_seq(TOPIC_TIMELINE) == 0

    verify = factory()
    try:
        refreshed = verify.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed.summary_title is None
        assert refreshed.anchor_title is None
        assert refreshed.summary_revision == 0
    finally:
        verify.close()
    reset_pubsub_for_test()


@pytest.mark.asyncio
async def test_generate_initial_title_impl_does_not_publish_without_first_user_message(tmp_path):
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session
    from zerg.services.session_summaries import generate_initial_title_impl

    reset_pubsub_for_test()
    factory = _make_db(tmp_path, "initial_title_no_first_user.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        user_messages=0,
        assistant_messages=1,
        transcript_revision=1,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    settings = SimpleNamespace(testing=False, llm_disabled=False)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            side_effect=AssertionError("provider should not be fetched when no first user message exists"),
        ),
    ):
        updated = await generate_initial_title_impl(session_id)

    assert updated is False
    assert get_pubsub().peek_latest_seq(topic_session(session_id)) == 0
    assert get_pubsub().peek_latest_seq(TOPIC_TIMELINE) == 0
    reset_pubsub_for_test()


@pytest.mark.asyncio
async def test_generate_initial_title_impl_skips_test_environment(tmp_path):
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session
    from zerg.services.session_summaries import generate_initial_title_impl

    reset_pubsub_for_test()
    factory = _make_db(tmp_path, "initial_title_test_environment.db")

    db = factory()
    session = AgentSession(
        provider="opencode",
        environment="test",
        project="longhouse-provider-live-proof",
        started_at=datetime.now(timezone.utc),
        first_user_message_preview="LONGHOUSE_OPENCODE_NOREPLY_skip_title",
        user_messages=1,
        assistant_messages=0,
        transcript_revision=1,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    settings = SimpleNamespace(testing=False, llm_disabled=False)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            side_effect=AssertionError("provider should not be fetched for test sessions"),
        ),
    ):
        updated = await generate_initial_title_impl(session_id)

    assert updated is False
    assert get_pubsub().peek_latest_seq(topic_session(session_id)) == 0
    assert get_pubsub().peek_latest_seq(TOPIC_TIMELINE) == 0

    verify = factory()
    try:
        refreshed = verify.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed.summary_title is None
        assert refreshed.summary_revision == 0
    finally:
        verify.close()
    reset_pubsub_for_test()


@pytest.mark.asyncio
async def test_generate_initial_title_impl_skips_blank_user_event(tmp_path, monkeypatch):
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_summaries import generate_initial_title_impl

    reset_pubsub_for_test()
    factory = _make_db(tmp_path, "initial_title_blank_user_event.db")

    db = factory()
    session = AgentSession(
        provider="cursor",
        environment="cinder",
        project="zeta",
        started_at=datetime.now(timezone.utc),
        user_messages=2,
        assistant_messages=0,
        transcript_revision=2,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.add(
        AgentEvent(
            session_id=session_id,
            role="user",
            content_text="",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.add(
        AgentEvent(
            session_id=session_id,
            role="user",
            content_text="Fix the Docker Rosetta emulation warning.",
            timestamp=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    db.commit()
    db.close()

    captured: dict[str, object] = {}

    async def _fake_generate_initial_session_title(**kwargs):
        captured.update(kwargs)
        return "Fix Docker Rosetta Emulation"

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr(
        "zerg.services.title_generator.generate_initial_session_title",
        _fake_generate_initial_session_title,
    )

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "deepseek/deepseek-v4-flash", "openrouter"),
        ),
    ):
        updated = await generate_initial_title_impl(session_id)

    assert updated is True
    assert captured["first_user_message"] == "Fix the Docker Rosetta emulation warning."

    verify = factory()
    try:
        refreshed = verify.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed.summary_title == "Fix Docker Rosetta Emulation"
        assert refreshed.anchor_title == "Fix Docker Rosetta Emulation"
    finally:
        verify.close()
    reset_pubsub_for_test()


@pytest.mark.asyncio
async def test_generate_initial_title_impl_does_not_overwrite_existing_title(tmp_path):
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session
    from zerg.services.session_summaries import generate_initial_title_impl

    reset_pubsub_for_test()
    factory = _make_db(tmp_path, "initial_title_existing.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        first_user_message_preview="Rename should not happen.",
        summary_title="Existing Title",
        transcript_revision=1,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()

    settings = SimpleNamespace(testing=False, llm_disabled=False)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            side_effect=AssertionError("provider should not be fetched when title already exists"),
        ),
    ):
        updated = await generate_initial_title_impl(session_id)

    assert updated is False
    assert get_pubsub().peek_latest_seq(topic_session(session_id)) == 0
    assert get_pubsub().peek_latest_seq(TOPIC_TIMELINE) == 0
    reset_pubsub_for_test()


@pytest.mark.asyncio
async def test_generate_embeddings_impl_skips_provider_when_embedding_revision_current(tmp_path):
    from zerg.services.session_summaries import generate_embeddings_impl

    factory = _make_db(tmp_path, "embedding_revision_current.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="cinder",
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
        environment="cinder",
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
        environment="cinder",
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
async def test_generate_summary_impl_bootstraps_from_bounded_message_tail(tmp_path, monkeypatch):
    import zerg.services.session_summaries as summaries

    factory = _make_db(tmp_path, "summary_bounded_tail.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=9,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    for idx in range(6):
        db.add(
            AgentEvent(
                session_id=session.id,
                role="user" if idx % 2 == 0 else "assistant",
                content_text=f"message-{idx}-long-text",
                timestamp=datetime.now(timezone.utc),
            )
        )
    db.add(
        AgentEvent(
            session_id=session.id,
            role="tool",
            content_text="tool result should be ignored",
            tool_output_text="x" * 100_000,
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    session_id = str(session.id)
    db.close()

    captured_events: list[dict] = []

    async def _fake_incremental_summary(**kwargs):
        captured_events.extend(kwargs["new_events"])
        return SimpleNamespace(summary="Summarized recent tail", title="Recent Tail")

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr(summaries, "SUMMARY_EVENT_LOAD_LIMIT", 3)
    monkeypatch.setattr(summaries, "SUMMARY_EVENT_TEXT_MAX_CHARS", 8)
    monkeypatch.setattr("zerg.services.session_processing.incremental_summary", _fake_incremental_summary)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "test-model", "test-provider"),
        ),
    ):
        await summaries.generate_summary_impl(session_id)

    assert [event["content_text"] for event in captured_events] == ["message-", "message-", "message-"]
    assert {event["role"] for event in captured_events} == {"assistant", "user"}
    assert all(event["tool_output_text"] is None for event in captured_events)

    verify_db = factory()
    refreshed = verify_db.query(AgentSession).filter(AgentSession.id == session_id).one()
    verify_db.close()

    assert refreshed.summary_revision == 9
    assert refreshed.last_summarized_event_id is not None
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_summary_impl_chunks_cursor_backlog_without_marking_current(tmp_path, monkeypatch):
    import zerg.services.session_summaries as summaries

    factory = _make_db(tmp_path, "summary_cursor_chunk.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=10,
        summary_revision=4,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    first = AgentEvent(
        session_id=session.id,
        role="user",
        content_text="already summarized",
        timestamp=datetime.now(timezone.utc),
    )
    db.add(first)
    db.commit()
    db.refresh(first)
    session.last_summarized_event_id = first.id
    for idx in range(5):
        db.add(
            AgentEvent(
                session_id=session.id,
                role="assistant" if idx % 2 else "user",
                content_text=f"new message {idx}",
                timestamp=datetime.now(timezone.utc),
            )
        )
    db.commit()
    session_id = str(session.id)
    cursor_id = first.id
    db.close()

    captured_events: list[dict] = []

    async def _fake_incremental_summary(**kwargs):
        captured_events.extend(kwargs["new_events"])
        return SimpleNamespace(summary="Chunk summary", title="Chunk")

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr(summaries, "SUMMARY_EVENT_LOAD_LIMIT", 2)
    monkeypatch.setattr("zerg.services.session_processing.incremental_summary", _fake_incremental_summary)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "test-model", "test-provider"),
        ),
    ):
        await summaries.generate_summary_impl(session_id)

    assert [event["content_text"] for event in captured_events] == ["new message 0", "new message 1"]

    verify_db = factory()
    refreshed = verify_db.query(AgentSession).filter(AgentSession.id == session_id).one()
    verify_db.close()

    assert refreshed.summary_revision == 4
    assert refreshed.last_summarized_event_id is not None
    assert refreshed.last_summarized_event_id > cursor_id
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_summary_impl_does_not_overwrite_with_placeholder_result(tmp_path, monkeypatch):
    from zerg.services.session_summaries import generate_summary_impl

    factory = _make_db(tmp_path, "summary_placeholder_result.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
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
        environment="cinder",
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
        environment="cinder",
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


# ---------------------------------------------------------------------------
# Distributed summary lock


@pytest.mark.asyncio
async def test_summary_lock_prevents_duplicate_llm_call(tmp_path, monkeypatch):
    """When another replica holds the lock, generate_summary_impl skips the LLM call."""
    import zerg.services.session_summaries as summaries

    factory = _make_db(tmp_path, "summary_lock_skip.db")

    db = factory()
    session = AgentSession(
        provider="claude",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=5,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    for idx in range(4):
        db.add(
            AgentEvent(
                session_id=session.id,
                role="user" if idx % 2 == 0 else "assistant",
                content_text=f"Message {idx}",
                timestamp=datetime.now(timezone.utc),
            )
        )
    db.commit()
    session_id = str(session.id)

    # Pre-claim the lock as a different replica
    from sqlalchemy import update

    db.execute(
        update(AgentSession)
        .where(AgentSession.id == session_id)
        .values(
            summary_lock_instance="other-replica:999",
            summary_lock_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.close()

    captured = []

    async def _fake_incremental(**kwargs):
        captured.append(kwargs)

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr("zerg.services.session_processing.incremental_summary", _fake_incremental)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "test-model", "test-provider"),
        ),
    ):
        await summaries.generate_summary_impl(session_id)

    assert captured == []  # LLM was never reached
    client.close.assert_awaited_once()  # cleaned up in finally

    verify_db = factory()
    session_after = verify_db.query(AgentSession).filter(AgentSession.id == session_id).first()
    verify_db.close()
    assert session_after.summary_lock_instance == "other-replica:999"  # lock untouched
    assert session_after.summary_revision == 0  # no progress


@pytest.mark.asyncio
async def test_summary_lock_stale_lock_is_broken(tmp_path, monkeypatch):
    """A stale lock (>5 min) is broken and the LLM call proceeds."""
    import zerg.services.session_summaries as summaries

    factory = _make_db(tmp_path, "summary_lock_stale.db")

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="cinder",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=5,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    for idx in range(4):
        db.add(
            AgentEvent(
                session_id=session.id,
                role="user" if idx % 2 == 0 else "assistant",
                content_text=f"Message {idx}",
                timestamp=datetime.now(timezone.utc),
            )
        )
    db.commit()
    session_id = str(session.id)

    # Pre-claim with a stale timestamp (>5 min ago)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    from sqlalchemy import update

    db.execute(
        update(AgentSession)
        .where(AgentSession.id == session_id)
        .values(
            summary_lock_instance="crashed-replica:1",
            summary_lock_at=stale_time,
        )
    )
    db.commit()
    db.close()

    captured = []

    async def _fake_incremental(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(summary="Fresh summary", title="Fresh Title")

    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(testing=False, llm_disabled=False)

    monkeypatch.setattr("zerg.services.session_processing.incremental_summary", _fake_incremental)

    with (
        patch("zerg.database.get_session_factory", return_value=factory),
        patch("zerg.services.session_summaries.get_settings", return_value=settings),
        patch(
            "zerg.models_config.get_llm_client_for_use_case",
            return_value=(client, "test-model", "test-provider"),
        ),
    ):
        await summaries.generate_summary_impl(session_id)

    assert len(captured) == 1  # LLM was called
    client.close.assert_awaited_once()

    verify_db = factory()
    session_after = verify_db.query(AgentSession).filter(AgentSession.id == session_id).first()
    verify_db.close()
    assert session_after.summary_lock_instance is None  # lock released
    assert session_after.summary_lock_at is None
    assert session_after.summary_revision == 5  # progress made
