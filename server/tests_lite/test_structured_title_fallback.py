"""Session response fallback behaviour and first-message projections."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base, make_engine, make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent, AgentSession, TimelineCard
from zerg.services.session_hot_cards import upsert_timeline_card_from_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name="test.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(
    factory,
    *,
    summary_title=None,
    project=None,
    git_branch=None,
    first_user_message_preview=None,
    last_user_message_preview=None,
    last_assistant_message_preview=None,
):
    db = factory()
    s = AgentSession(
        provider="claude",
        environment="production",
        project=project,
        git_branch=git_branch,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        summary_title=summary_title,
        first_user_message_preview=first_user_message_preview,
        last_user_message_preview=last_user_message_preview,
        last_assistant_message_preview=last_assistant_message_preview,
    )
    db.add(s)
    db.flush()
    upsert_timeline_card_from_session(db, s)
    db.commit()
    db.refresh(s)
    db.close()
    return s


def _seed_event(factory, session_id, *, role="user", content="hello"):
    db = factory()
    e = AgentEvent(
        session_id=session_id,
        role=role,
        content_text=content,
        timestamp=datetime.now(timezone.utc),
    )
    db.add(e)
    db.commit()
    db.close()
    return e


# ---------------------------------------------------------------------------
# Tests: first_user_message in sessions list response
# ---------------------------------------------------------------------------


def test_sessions_list_includes_first_user_message(tmp_path):
    """GET /api/agents/sessions returns first_user_message for each session."""
    from fastapi.testclient import TestClient

    from zerg.database import Base, get_db, make_engine, make_sessionmaker
    from zerg.main import api_app

    db_path = tmp_path / "test_first_msg.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    session = _seed_session(
        factory,
        project="proj",
        git_branch="feat",
        first_user_message_preview="First question here",
    )
    _seed_event(factory, session.id, role="user", content="First question here")
    _seed_event(factory, session.id, role="assistant", content="Answer")
    _seed_event(factory, session.id, role="user", content="Second question")

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="structured-title", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    try:
        client = TestClient(api_app)
        with patch(
            "zerg.services.agents.store.AgentsStore.get_first_message_map",
            side_effect=AssertionError("session list must use hot preview columns"),
        ):
            resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["first_user_message"] == "First question here"
    finally:
        api_app.dependency_overrides.clear()


def test_sessions_list_uses_preview_backfill_for_existing_rows(tmp_path):
    """Legacy rows need an explicit backfill; request-time lists stay hot-only."""
    from fastapi.testclient import TestClient

    from zerg.database import Base, get_db, make_engine, make_sessionmaker
    from zerg.main import api_app
    from zerg.services.session_preview_backfill import backfill_missing_session_previews

    db_path = tmp_path / "test_first_msg_legacy.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    session = _seed_session(
        factory,
        project="proj",
        git_branch="feat",
        first_user_message_preview=None,
    )
    _seed_event(factory, session.id, role="user", content="Legacy first question")
    _seed_event(factory, session.id, role="assistant", content="Legacy answer")

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="structured-title", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    try:
        client = TestClient(api_app)
        with patch(
            "zerg.services.agents.store.AgentsStore.get_first_message_map",
            side_effect=AssertionError("session list must not query legacy events for missing previews"),
        ):
            resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["first_user_message"] is None

        db = factory()
        try:
            result = backfill_missing_session_previews(db, limit=10)
            db.commit()
        finally:
            db.close()

        assert result.selected_sessions == 1
        assert result.updated_sessions == 1
        assert result.first_user_filled == 1
        assert result.last_visible_filled == 1
        assert result.last_user_filled == 1
        assert result.last_assistant_filled == 1

        db = factory()
        try:
            repaired = db.query(AgentSession).filter(AgentSession.id == session.id).one()
            card = db.query(TimelineCard).filter(TimelineCard.session_id == session.id).one()
        finally:
            db.close()
        assert repaired.last_user_message_preview == "Legacy first question"
        assert repaired.last_assistant_message_preview == "Legacy answer"
        assert card.last_user_message_preview == "Legacy first question"
        assert card.last_assistant_message_preview == "Legacy answer"

        with patch(
            "zerg.services.agents.store.AgentsStore.get_first_message_map",
            side_effect=AssertionError("session list must use backfilled hot preview columns"),
        ):
            resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["first_user_message"] == "Legacy first question"
    finally:
        api_app.dependency_overrides.clear()


def test_preview_backfill_creates_missing_timeline_card_for_hot_legacy_row(tmp_path):
    from zerg.services.session_preview_backfill import backfill_missing_session_previews

    factory = _make_db(tmp_path, "legacy_hot_row_missing_card.db")
    session = _seed_session(
        factory,
        project="proj",
        git_branch="feat",
        first_user_message_preview="Already hot",
    )

    db = factory()
    try:
        existing = db.query(AgentSession).filter(AgentSession.id == session.id).one()
        existing.last_visible_text_preview = "Already latest"
        existing.last_user_message_preview = "Already last user"
        existing.last_assistant_message_preview = "Already last assistant"
        db.query(TimelineCard).filter(TimelineCard.session_id == session.id).delete()
        db.commit()

        result = backfill_missing_session_previews(db, limit=10)
        db.commit()

        card = db.query(TimelineCard).filter(TimelineCard.session_id == session.id).one()
    finally:
        db.close()

    assert result.selected_sessions == 1
    assert result.updated_sessions == 0
    assert result.updated_timeline_cards == 1
    assert result.last_user_filled == 0
    assert result.last_assistant_filled == 0
    assert card.first_user_message_preview == "Already hot"
    assert card.last_visible_text_preview == "Already latest"
    assert card.last_user_message_preview == "Already last user"
    assert card.last_assistant_message_preview == "Already last assistant"
