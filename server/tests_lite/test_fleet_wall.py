"""Tests for fleet wall and session tail endpoints.

Covers:
- GET /agents/sessions/wall — raw signal metadata with repo/project filters
- GET /agents/sessions/{id}/tail — tail-biased recent events
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
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.models.agents import SessionRuntimeState

# ---------------------------------------------------------------------------
# DB / client helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_fleet_wall.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
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
            cwd="/Users/dev/git/zerg",
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
        assert session["cwd"] == "/Users/dev/git/zerg"
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

def test_wall_repo_filter_matches_cwd(tmp_path):
    """Wall repo filter also matches against cwd for non-git workspaces."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_session(db, cwd="/Users/dev/git/acme/project", git_repo=None, project="project")
        _seed_session(db, cwd="/Users/dev/git/zerg", git_repo="https://github.com/user/other", project="zerg")

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall", params={"repo": "acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["sessions"][0]["cwd"] == "/Users/dev/git/acme/project"
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
        target = _seed_session(db, device_id="shipper-demo", device_name="demo-machine", git_repo="repo-b")
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
        session = next(s for s in data["sessions"] if s["device_name"] == "demo-machine")
        assert session["pending_inbound_messages"] == 1
    finally:
        api_ref.dependency_overrides = {}


def test_wall_uses_runtime_state_for_live_presence_without_presence_row(tmp_path):
    """Wall uses live runtime state as the single source of presence truth."""
    SessionLocal = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(
            db,
            provider="codex",
            device_id="shipper-demo",
            device_name="demo-machine",
            git_repo="runtime-only",
            project="zerg",
            last_activity_at=now - timedelta(seconds=20),
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"codex:{session.id}",
                session_id=session.id,
                provider="codex",
                device_id="shipper-demo",
                phase="needs_user",
                phase_source="semantic",
                active_tool=None,
                phase_started_at=now - timedelta(seconds=20),
                last_runtime_signal_at=now - timedelta(seconds=20),
                last_progress_at=now - timedelta(seconds=20),
                last_live_at=now - timedelta(seconds=20),
                timeline_anchor_at=now - timedelta(seconds=20),
                freshness_expires_at=now + timedelta(minutes=5),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall")
        assert resp.status_code == 200
        data = resp.json()
        row = next(s for s in data["sessions"] if s["device_name"] == "demo-machine")
        assert row["has_live_presence"] is True
        assert row["presence_state"] == "needs_user"
    finally:
        api_ref.dependency_overrides = {}


def test_wall_device_name_falls_back_from_device_id(tmp_path):
    """If device_name is null, wall derives it by stripping 'shipper-' from device_id."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_session(db, device_id="shipper-demo", device_name=None, git_repo="r")

    client, api_ref = _make_client(SessionLocal)
    try:
        resp = client.get("/api/agents/sessions/wall")
        data = resp.json()
        session = data["sessions"][0]
        assert session["device_name"] == "demo"
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
