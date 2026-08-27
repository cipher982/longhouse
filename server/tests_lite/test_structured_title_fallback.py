"""Session response fallback behaviour and first-message projections."""

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import TimelineCard
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


def test_sessions_list_includes_first_user_message(live_catalog, live_catalog_client):
    """GET /api/agents/sessions returns first_user_message for each session.

    The preview is the one the render manifest carried into the catalog when the
    transcript was sealed; the listing never re-reads the transcript for it.
    """

    owner_id = live_catalog.create_user("owner@example.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="structured-title")
    live_catalog.commit_session(
        owner_id=owner_id,
        device_id="structured-title",
        project="proj",
        texts=("First question here", "Second question"),
    )

    resp = live_catalog_client.get("/agents/sessions", headers={"X-Agents-Token": token})
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["first_user_message"] == "First question here"


def test_preview_backfill_fills_missing_previews_from_archive_events(tmp_path):
    """Archive rows predating the preview columns are repaired from their events."""
    from zerg.services.session_preview_backfill import backfill_missing_session_previews

    factory = _make_db(tmp_path, "test_first_msg_legacy.db")

    session = _seed_session(
        factory,
        project="proj",
        git_branch="feat",
        first_user_message_preview=None,
    )
    _seed_event(factory, session.id, role="user", content="Legacy first question")
    _seed_event(factory, session.id, role="assistant", content="Legacy answer")

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
    assert repaired.first_user_message_preview == "Legacy first question"
    assert repaired.last_user_message_preview == "Legacy first question"
    assert repaired.last_assistant_message_preview == "Legacy answer"
    assert card.first_user_message_preview == "Legacy first question"
    assert card.last_user_message_preview == "Legacy first question"
    assert card.last_assistant_message_preview == "Legacy answer"


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


def test_preview_backfill_keeps_latest_assistant_in_last_visible_projection(tmp_path):
    from zerg.services.session_preview_backfill import backfill_missing_session_previews

    factory = _make_db(tmp_path, "semantic_last_visible.db")
    session = _seed_session(
        factory,
        first_user_message_preview=None,
        last_user_message_preview=None,
        last_assistant_message_preview=None,
    )
    base = datetime.now(timezone.utc)
    db = factory()
    try:
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="real prompt",
                    timestamp=base,
                    interaction_kind="durable_user_message",
                    title_eligible=1,
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="latest answer",
                    timestamp=base + timedelta(seconds=1),
                    interaction_kind="provider_system",
                    title_eligible=0,
                ),
            ]
        )
        db.commit()

        result = backfill_missing_session_previews(db, limit=10)
        db.commit()
        repaired = db.query(AgentSession).filter(AgentSession.id == session.id).one()
    finally:
        db.close()

    assert result.last_visible_filled == 1
    assert repaired.last_visible_text_preview == "latest answer"
