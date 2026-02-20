"""Tests for _set_structured_title_if_empty and session title fallback behaviour.

Covers:
- Structured title is set from project · branch when no LLM title exists
- Existing LLM title is NOT overwritten (WHERE summary_title IS NULL guard)
- No title set when session has no project or branch
- first_user_message included in sessions list response
"""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import make_engine, make_sessionmaker
from zerg.models.agents import AgentEvent, AgentSession, AgentsBase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name="test.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(factory, *, summary_title=None, project=None, git_branch=None):
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
    )
    db.add(s)
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
# Tests: _set_structured_title_if_empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_title_set_from_project_and_branch(tmp_path):
    """Sets summary_title from project · branch when no title exists."""
    from zerg.routers.agents import _set_structured_title_if_empty

    factory = _make_db(tmp_path, "proj_branch.db")
    session = _seed_session(factory, project="myproject", git_branch="main")

    with patch("zerg.database.get_session_factory", return_value=factory):
        await _set_structured_title_if_empty(str(session.id))

    db = factory()
    updated = db.query(AgentSession).filter(AgentSession.id == session.id).first()
    db.close()
    assert updated.summary_title == "myproject · main"


@pytest.mark.asyncio
async def test_structured_title_project_only(tmp_path):
    """Sets summary_title from project alone when no branch."""
    from zerg.routers.agents import _set_structured_title_if_empty

    factory = _make_db(tmp_path, "proj_only.db")
    session = _seed_session(factory, project="myproject", git_branch=None)

    with patch("zerg.database.get_session_factory", return_value=factory):
        await _set_structured_title_if_empty(str(session.id))

    db = factory()
    updated = db.query(AgentSession).filter(AgentSession.id == session.id).first()
    db.close()
    assert updated.summary_title == "myproject"


@pytest.mark.asyncio
async def test_structured_title_does_not_overwrite_existing(tmp_path):
    """WHERE summary_title IS NULL: existing title must not be overwritten."""
    from zerg.routers.agents import _set_structured_title_if_empty

    factory = _make_db(tmp_path, "existing_title.db")
    session = _seed_session(
        factory,
        project="myproject",
        git_branch="main",
        summary_title="Real LLM Title",
    )

    with patch("zerg.database.get_session_factory", return_value=factory):
        await _set_structured_title_if_empty(str(session.id))

    db = factory()
    updated = db.query(AgentSession).filter(AgentSession.id == session.id).first()
    db.close()
    assert updated.summary_title == "Real LLM Title"


@pytest.mark.asyncio
async def test_structured_title_skipped_when_no_project_or_branch(tmp_path):
    """No title set when session has neither project nor branch."""
    from zerg.routers.agents import _set_structured_title_if_empty

    factory = _make_db(tmp_path, "no_meta.db")
    session = _seed_session(factory, project=None, git_branch=None)

    with patch("zerg.database.get_session_factory", return_value=factory):
        await _set_structured_title_if_empty(str(session.id))

    db = factory()
    updated = db.query(AgentSession).filter(AgentSession.id == session.id).first()
    db.close()
    assert updated.summary_title is None


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
    AgentsBase.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    session = _seed_session(factory, project="proj", git_branch="feat")
    _seed_event(factory, session.id, role="user", content="First question here")
    _seed_event(factory, session.id, role="assistant", content="Answer")
    _seed_event(factory, session.id, role="user", content="Second question")

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    api_app.dependency_overrides[get_db] = override
    try:
        client = TestClient(api_app)
        resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["first_user_message"] == "First question here"
    finally:
        api_app.dependency_overrides.clear()
