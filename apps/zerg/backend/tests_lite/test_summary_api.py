"""HTTP-level tests for summary fields in session API responses.

Covers:
- GET /api/agents/sessions returns summary + summary_title fields
- GET /api/agents/sessions/{id} returns summary + summary_title fields
- Sessions without summary return null (not error)

Uses in-memory SQLite with inline setup (no shared conftest).
"""

from datetime import datetime
from datetime import timezone

from fastapi.testclient import TestClient

from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    """Create an in-memory SQLite DB with agent tables, return session factory."""
    db_path = tmp_path / "test_summary_api.db"
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, *, summary=None, summary_title=None, project="test-project", environment="production"):
    """Create a session with optional summary fields."""
    session = AgentSession(
        provider="claude",
        environment=environment,
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
            environment="work-macbook",
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
        assert session["environment"] == "work-macbook"
        assert session["thread_root_session_id"] == session["id"]
        assert session["thread_head_session_id"] == session["id"]
        assert session["thread_continuation_count"] == 1
        assert session["continuation_kind"] == "local"
        assert session["origin_label"] == "work-macbook"
        assert session["is_writable_head"] is True


def test_get_session_includes_summary(tmp_path):
    """GET /agents/sessions/{id} returns summary and summary_title fields."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            summary="Fixed critical database bug.",
            summary_title="Database Bug Fix",
            environment="work-laptop",
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
        assert data["environment"] == "work-laptop"
        assert data["thread_root_session_id"] == session_id
        assert data["thread_head_session_id"] == session_id
        assert data["thread_continuation_count"] == 1
        assert data["continuation_kind"] == "local"
        assert data["origin_label"] == "work-laptop"
        assert data["is_writable_head"] is True


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


def test_get_session_thread_returns_lineage(tmp_path):
    """GET /agents/sessions/{id}/thread returns the logical thread and head."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        root = _seed_session(
            db,
            summary="Started locally.",
            summary_title="Local root",
            environment="Cinder",
        )
        root.thread_root_session_id = root.id
        root.continuation_kind = "local"
        root.origin_label = "Cinder"
        root.is_writable_head = 1
        db.commit()

        store = AgentsStore(db)
        child = store.create_continuation_session(
            root.id,
            continuation_kind="cloud",
            origin_label="Cloud",
            environment="Cloud",
            device_id="zerg-commis-cloud",
            branched_from_event_id=None,
        )
        child.summary = "Continued in cloud."
        child.summary_title = "Cloud branch"
        db.commit()
        root_id = str(root.id)
        child_id = str(child.id)
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get(f"/agents/sessions/{root_id}/thread")
        assert resp.status_code == 200
        data = resp.json()
        assert data["root_session_id"] == root_id
        assert data["head_session_id"] == child_id
        assert len(data["sessions"]) == 2
        assert [item["id"] for item in data["sessions"]] == [root_id, child_id]
        assert data["sessions"][0]["is_writable_head"] is False
        assert data["sessions"][1]["is_writable_head"] is True
        assert data["sessions"][1]["continued_from_session_id"] == root_id
        assert data["sessions"][1]["origin_label"] == "Cloud"
        assert data["sessions"][1]["continuation_kind"] == "cloud"
