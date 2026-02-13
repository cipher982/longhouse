"""HTTP-level tests for summary fields in session API responses.

Covers:
- GET /api/agents/sessions returns summary + summary_title fields
- GET /api/agents/sessions/{id} returns summary + summary_title fields
- Sessions without summary return null (not error)

Uses in-memory SQLite with inline setup (no shared conftest).
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import get_db, make_engine, make_sessionmaker
from zerg.models.agents import AgentSession, AgentsBase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    """Create an in-memory SQLite DB with agent tables, return session factory."""
    db_path = tmp_path / "test_summary_api.db"
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, *, summary=None, summary_title=None, project="test-project"):
    """Create a session with optional summary fields."""
    session = AgentSession(
        provider="claude",
        environment="production",
        project=project,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=5,
        assistant_messages=7,
        tool_calls=3,
        summary=summary,
        summary_title=summary_title,
        summary_event_count=10 if summary else 0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _get_client(session_factory):
    """Create a TestClient with DB dependency override."""
    from zerg.main import api_app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_get_db
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_sessions_includes_summary(tmp_path):
    """GET /agents/sessions returns summary and summary_title fields."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        _seed_session(
            db,
            summary="Implemented JWT auth and rate limiting.",
            summary_title="Auth and Rate Limiting",
        )
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get("/agents/sessions?days_back=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sessions"]) >= 1
        session = data["sessions"][0]
        assert session["summary"] == "Implemented JWT auth and rate limiting."
        assert session["summary_title"] == "Auth and Rate Limiting"


def test_get_session_includes_summary(tmp_path):
    """GET /agents/sessions/{id} returns summary and summary_title fields."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            summary="Fixed critical database bug.",
            summary_title="Database Bug Fix",
        )
        session_id = str(session.id)
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get(f"/agents/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] == "Fixed critical database bug."
        assert data["summary_title"] == "Database Bug Fix"


def test_summary_null_when_missing(tmp_path):
    """Sessions without summary return null, not error."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(db, summary=None, summary_title=None)
        session_id = str(session.id)
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get(f"/agents/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] is None
        assert data["summary_title"] is None
