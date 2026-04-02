"""Tests for fleet wall, session tail, and pokes endpoints.

Covers:
- GET /agents/sessions/wall — raw signal metadata with repo/project filters
- GET /agents/sessions/{id}/tail — tail-biased recent events
- POST /agents/pokes — create a poke between sessions
- GET /agents/pokes — list pokes for a session, marks as read

Uses in-memory SQLite. No shared conftest.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import get_db
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.models.agents import SessionPoke

# ---------------------------------------------------------------------------
# DB / client helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_fleet_wall.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_client(SessionLocal):
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify():
        return SimpleNamespace(device_id="testclient", id="token-1")

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify
    api_app.dependency_overrides[require_single_tenant] = lambda: None

    client = TestClient(app, backend="asyncio")
    return client, api_app


def _seed_session(db, **kwargs):
    """Insert a session with sensible defaults, return it."""
    defaults = {
        "id": uuid4(),
        "provider": "claude",
        "environment": "development",
        "started_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "user_messages": 5,
        "assistant_messages": 10,
        "tool_calls": 3,
    }
    defaults.update(kwargs)
    s = AgentSession(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _seed_event(db, session_id, role="assistant", content="hello", tool_name=None, minutes_ago=0):
    """Insert an event, return it."""
    e = AgentEvent(
        session_id=session_id,
        role=role,
        content_text=content,
        tool_name=tool_name,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


# ---------------------------------------------------------------------------
# Wall endpoint tests
# ---------------------------------------------------------------------------


def test_wall_returns_sessions(tmp_path):
    """GET /agents/sessions/wall returns sessions with raw signal metadata."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _seed_session(
            db,
            device_id="shipper-laptop",
            device_name="laptop",
            git_repo="https://github.com/user/repo",
            git_branch="main",
            project="zerg",
        )
        _seed_event(db, s.id, role="user", content="fix the bug")

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        session = next(s for s in data["sessions"] if s["device_name"] == "laptop")
        assert session["git_repo"] == "https://github.com/user/repo"
        assert session["git_branch"] == "main"
        assert session["project"] == "zerg"
        assert session["provider"] == "claude"
        assert session["user_messages"] == 5
    finally:
        api_ref.dependency_overrides = {}


def test_wall_filters_by_repo(tmp_path):
    """Wall query repo filter does substring match."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_session(db, git_repo="https://github.com/user/zerg", project="zerg")
        _seed_session(db, git_repo="https://github.com/user/other", project="other")

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall", params={"repo": "zerg"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert "zerg" in data["sessions"][0]["git_repo"]
    finally:
        api_ref.dependency_overrides = {}


def test_wall_filters_by_project(tmp_path):
    """Wall query project filter returns only matching sessions."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_session(db, project="zerg", git_repo="a")
        _seed_session(db, project="hdr", git_repo="b")

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall", params={"project": "zerg"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["sessions"][0]["project"] == "zerg"
    finally:
        api_ref.dependency_overrides = {}


def test_wall_includes_pending_inbound_message_count(tmp_path):
    """Wall query surfaces unacknowledged inbound message counts."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        source = _seed_session(db, device_id="shipper-laptop", device_name="laptop", git_repo="repo-a")
        target = _seed_session(db, device_id="shipper-cube", device_name="cube", git_repo="repo-b")
        db.add_all(
            [
                SessionMessage(
                    from_session_id=source.id,
                    to_session_id=target.id,
                    body="queued work",
                    delivery_status="stored_only",
                ),
                SessionMessage(
                    from_session_id=source.id,
                    to_session_id=target.id,
                    body="already handled",
                    delivery_status="delivered",
                    acknowledged_at=datetime.now(timezone.utc),
                ),
                SessionMessage(
                    from_session_id=source.id,
                    to_session_id=target.id,
                    body="failed delivery",
                    delivery_status="failed",
                ),
            ]
        )
        db.commit()

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall")
        assert resp.status_code == 200
        data = resp.json()
        session = next(s for s in data["sessions"] if s["device_name"] == "cube")
        assert session["pending_inbound_messages"] == 1
    finally:
        api_ref.dependency_overrides = {}


def test_wall_device_name_falls_back_from_device_id(tmp_path):
    """If device_name is null, wall derives it by stripping 'shipper-' from device_id."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_session(db, device_id="shipper-cube", device_name=None, git_repo="r")

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall")
        data = resp.json()
        session = data["sessions"][0]
        assert session["device_name"] == "cube"
    finally:
        api_ref.dependency_overrides = {}


def test_wall_excludes_old_sessions(tmp_path):
    """Sessions older than the days param are excluded."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_session(
            db,
            started_at=datetime.now(timezone.utc) - timedelta(days=10),
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=10),
            git_repo="old",
        )
        _seed_session(db, git_repo="recent")

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall", params={"days": 3})
        data = resp.json()
        repos = [s["git_repo"] for s in data["sessions"]]
        assert "recent" in repos
        assert "old" not in repos
    finally:
        api_ref.dependency_overrides = {}


# ---------------------------------------------------------------------------
# Session tail tests
# ---------------------------------------------------------------------------


def test_tail_returns_404_for_missing_session(tmp_path):
    """GET /agents/sessions/{id}/tail returns 404 for nonexistent session."""
    SessionLocal = _make_db(tmp_path)
    client, api_ref = _make_client(SessionLocal)
    try:
        fake_id = str(uuid4())
        resp = client.get(f"/api/agents/sessions/{fake_id}/tail")
        assert resp.status_code == 404
    finally:
        api_ref.dependency_overrides = {}


def test_tail_returns_recent_events(tmp_path):
    """GET /agents/sessions/{id}/tail returns last N events in chronological order."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _seed_session(db)
        _seed_event(db, s.id, role="user", content="first message", minutes_ago=10)
        _seed_event(db, s.id, role="assistant", content="second response", minutes_ago=5)
        _seed_event(db, s.id, role="user", content="third message", minutes_ago=1)
        sid = str(s.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get(f"/api/agents/sessions/{sid}/tail", params={"limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == sid
        assert len(data["events"]) == 2
        # Chronological order (oldest first)
        assert data["events"][0]["content"] == "second response"
        assert data["events"][1]["content"] == "third message"
    finally:
        api_ref.dependency_overrides = {}


def test_tail_filters_to_user_assistant_tool(tmp_path):
    """Tail only returns user, assistant, and tool role events."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _seed_session(db)
        _seed_event(db, s.id, role="user", content="visible")
        _seed_event(db, s.id, role="system", content="hidden")
        _seed_event(db, s.id, role="assistant", content="also visible")
        sid = str(s.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get(f"/api/agents/sessions/{sid}/tail")
        data = resp.json()
        roles = [e["role"] for e in data["events"]]
        assert "system" not in roles
        assert "user" in roles
        assert "assistant" in roles
    finally:
        api_ref.dependency_overrides = {}


def test_tail_truncates_long_content(tmp_path):
    """Content longer than 4000 chars is truncated."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _seed_session(db)
        _seed_event(db, s.id, role="user", content="x" * 8000)
        sid = str(s.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get(f"/api/agents/sessions/{sid}/tail")
        data = resp.json()
        assert len(data["events"][0]["content"]) == 4000
    finally:
        api_ref.dependency_overrides = {}


def test_tail_includes_tool_name(tmp_path):
    """Tool events include the tool_name field."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _seed_session(db)
        _seed_event(db, s.id, role="tool", content="output", tool_name="Bash")
        sid = str(s.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get(f"/api/agents/sessions/{sid}/tail")
        data = resp.json()
        assert data["events"][0]["tool_name"] == "Bash"
    finally:
        api_ref.dependency_overrides = {}


# ---------------------------------------------------------------------------
# Poke tests
# ---------------------------------------------------------------------------


def test_create_poke(tmp_path):
    """POST /agents/pokes creates a poke between sessions."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s1 = _seed_session(db)
        s2 = _seed_session(db)
        s1_id, s2_id = str(s1.id), str(s2.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.post(
            "/api/agents/pokes",
            json={
                "from_session_id": s1_id,
                "to_session_id": s2_id,
                "note": "auth rotation is broken, don't test auth",
                "source_event_id": 42,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["from_session_id"] == s1_id
        assert data["to_session_id"] == s2_id
        assert data["note"] == "auth rotation is broken, don't test auth"
        assert data["source_event_id"] == 42
        assert data["id"] is not None
    finally:
        api_ref.dependency_overrides = {}


def test_create_poke_truncates_long_note(tmp_path):
    """Notes over 2000 chars are truncated."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s1 = _seed_session(db)
        s2 = _seed_session(db)
        s1_id, s2_id = str(s1.id), str(s2.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.post(
            "/api/agents/pokes",
            json={
                "from_session_id": s1_id,
                "to_session_id": s2_id,
                "note": "z" * 5000,
            },
        )
        assert resp.status_code == 201
        assert len(resp.json()["note"]) == 2000
    finally:
        api_ref.dependency_overrides = {}


def test_list_pokes_returns_unread(tmp_path):
    """GET /agents/pokes returns unread pokes for a session."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s1 = _seed_session(db)
        s2 = _seed_session(db)
        poke = SessionPoke(
            from_session_id=s1.id,
            to_session_id=s2.id,
            note="hey look at this",
        )
        db.add(poke)
        db.commit()
        s2_id = str(s2.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/pokes", params={"session_id": s2_id})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["pokes"][0]["note"] == "hey look at this"
    finally:
        api_ref.dependency_overrides = {}


def test_list_pokes_marks_as_read(tmp_path):
    """Fetching pokes marks them as read — second call returns empty."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s1 = _seed_session(db)
        s2 = _seed_session(db)
        poke = SessionPoke(
            from_session_id=s1.id,
            to_session_id=s2.id,
            note="one-time ping",
        )
        db.add(poke)
        db.commit()
        s2_id = str(s2.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        # First call — should see the poke
        resp1 = client.get("/api/agents/pokes", params={"session_id": s2_id})
        assert resp1.json()["total"] == 1

        # Second call — should be empty (unread_only=true by default)
        resp2 = client.get("/api/agents/pokes", params={"session_id": s2_id})
        assert resp2.json()["total"] == 0
    finally:
        api_ref.dependency_overrides = {}


def test_list_pokes_unread_only_false_shows_all(tmp_path):
    """With unread_only=false, already-read pokes still appear."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s1 = _seed_session(db)
        s2 = _seed_session(db)
        poke = SessionPoke(
            from_session_id=s1.id,
            to_session_id=s2.id,
            note="persistent",
            read_at=datetime.now(timezone.utc),
        )
        db.add(poke)
        db.commit()
        s2_id = str(s2.id)

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get(
            "/api/agents/pokes",
            params={"session_id": s2_id, "unread_only": "false"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
    finally:
        api_ref.dependency_overrides = {}
