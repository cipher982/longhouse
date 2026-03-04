"""TDD tests for extended presence states: needs_user and blocked.

These tests were written BEFORE the implementation.  They define the
exact contract for the new states and must pass after implementation.

States being added:
  - needs_user  — Claude is waiting for user input (idle_prompt /
                  elicitation_dialog Notification)
  - blocked     — Claude is waiting for permission approval
                  (PermissionRequest / permission_prompt Notification)

Auto-resume behaviour difference from existing states:
  - thinking / running  → auto-resume snoozed sessions (session is active)
  - needs_user / blocked → do NOT auto-resume (session is paused,
    user needs to come back deliberately)

tool_name storage:
  - running     → store tool_name (active tool execution)
  - blocked     → store tool_name (blocked waiting on this specific tool)
  - needs_user  → clear tool_name (no active tool, just waiting for input)
  - thinking / idle → clear tool_name

derived_status mapping (used by active-sessions endpoint):
  - thinking / running / needs_user / blocked → "working"
  - idle → "idle"
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import get_db, make_engine, make_sessionmaker  # noqa: E402
from zerg.main import api_app  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.agents import AgentsBase  # noqa: E402
from zerg.models.agents import SessionPresence  # noqa: E402


# ---------------------------------------------------------------------------
# DB + client fixtures (same pattern as other tests_lite tests)
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name="test.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    AgentsBase.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


@pytest.fixture()
def db_session(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def client(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    with TestClient(api_app) as c:
        yield c
    api_app.dependency_overrides.clear()
    engine.dispose()


def _auth_headers() -> dict:
    return {"X-Agents-Token": "test-token"}


def _make_session(db, sid: str | None = None) -> AgentSession:
    """Create a minimal AgentSession row."""
    if sid is None:
        sid = str(uuid4())
    s = AgentSession(
        id=sid,
        provider="claude",
        project="test-project",
        environment="test-machine",
        started_at=datetime.now(timezone.utc),
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ---------------------------------------------------------------------------
# 1. Backend: VALID_STATES accepts new states
# ---------------------------------------------------------------------------


def test_needs_user_state_accepted(client):
    """POST presence with state=needs_user returns 204."""
    sid = str(uuid4())
    resp = client.post(
        "/agents/presence",
        json={"session_id": sid, "state": "needs_user", "cwd": "/tmp/test"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 204


def test_blocked_state_accepted(client):
    """POST presence with state=blocked returns 204."""
    sid = str(uuid4())
    resp = client.post(
        "/agents/presence",
        json={
            "session_id": sid,
            "state": "blocked",
            "tool_name": "Bash",
            "cwd": "/tmp/test",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 204


def test_unknown_state_still_ignored(client):
    """Unknown states must still return 204 (silent ignore, don't break hooks)."""
    sid = str(uuid4())
    resp = client.post(
        "/agents/presence",
        json={"session_id": sid, "state": "completely_unknown", "cwd": "/tmp/test"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# 2. Backend: tool_name storage semantics
# ---------------------------------------------------------------------------


def test_blocked_stores_tool_name(client, tmp_path):
    """blocked state stores tool_name (blocked on a specific tool)."""
    engine, SessionLocal = _make_db(tmp_path)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    with TestClient(api_app) as c:
        sid = str(uuid4())
        c.post(
            "/agents/presence",
            json={
                "session_id": sid,
                "state": "blocked",
                "tool_name": "Bash",
                "cwd": "/tmp/test",
            },
            headers=_auth_headers(),
        )
        db = SessionLocal()
        row = db.query(SessionPresence).filter(SessionPresence.session_id == sid).first()
        assert row is not None
        assert row.tool_name == "Bash", f"expected tool_name='Bash', got {row.tool_name!r}"
        db.close()
    api_app.dependency_overrides.clear()
    engine.dispose()


def test_needs_user_clears_tool_name(client, tmp_path):
    """needs_user state clears tool_name (no active tool execution)."""
    engine, SessionLocal = _make_db(tmp_path)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    with TestClient(api_app) as c:
        sid = str(uuid4())
        # First set running with a tool
        c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "running", "tool_name": "Bash", "cwd": "/tmp"},
            headers=_auth_headers(),
        )
        # Then transition to needs_user — should clear tool_name
        c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "needs_user", "cwd": "/tmp"},
            headers=_auth_headers(),
        )
        db = SessionLocal()
        row = db.query(SessionPresence).filter(SessionPresence.session_id == sid).first()
        assert row is not None
        assert row.tool_name is None, f"expected tool_name=None on needs_user, got {row.tool_name!r}"
        db.close()
    api_app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# 3. Backend: auto-resume behaviour
# ---------------------------------------------------------------------------


def test_needs_user_does_not_auto_resume_snoozed(client, tmp_path):
    """needs_user must NOT auto-resume a snoozed session."""
    engine, SessionLocal = _make_db(tmp_path)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    with TestClient(api_app) as c:
        db = SessionLocal()
        session = _make_session(db)
        # Snooze the session
        db.query(AgentSession).filter(AgentSession.id == session.id).update(
            {"user_state": "snoozed"}, synchronize_session=False
        )
        db.commit()

        c.post(
            "/agents/presence",
            json={"session_id": str(session.id), "state": "needs_user", "cwd": "/tmp"},
            headers=_auth_headers(),
        )

        db.expire_all()
        updated = db.query(AgentSession).filter(AgentSession.id == session.id).first()
        assert updated.user_state == "snoozed", (
            f"needs_user should NOT auto-resume snoozed session, got user_state={updated.user_state!r}"
        )
        db.close()
    api_app.dependency_overrides.clear()
    engine.dispose()


def test_blocked_does_not_auto_resume_snoozed(client, tmp_path):
    """blocked must NOT auto-resume a snoozed session."""
    engine, SessionLocal = _make_db(tmp_path)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    with TestClient(api_app) as c:
        db = SessionLocal()
        session = _make_session(db)
        db.query(AgentSession).filter(AgentSession.id == session.id).update(
            {"user_state": "snoozed"}, synchronize_session=False
        )
        db.commit()

        c.post(
            "/agents/presence",
            json={"session_id": str(session.id), "state": "blocked", "cwd": "/tmp"},
            headers=_auth_headers(),
        )

        db.expire_all()
        updated = db.query(AgentSession).filter(AgentSession.id == session.id).first()
        assert updated.user_state == "snoozed", (
            f"blocked should NOT auto-resume snoozed session, got user_state={updated.user_state!r}"
        )
        db.close()
    api_app.dependency_overrides.clear()
    engine.dispose()


def test_thinking_still_auto_resumes_snoozed(client, tmp_path):
    """thinking still auto-resumes snoozed sessions (existing behaviour must not regress)."""
    engine, SessionLocal = _make_db(tmp_path)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    with TestClient(api_app) as c:
        db = SessionLocal()
        session = _make_session(db)
        db.query(AgentSession).filter(AgentSession.id == session.id).update(
            {"user_state": "snoozed"}, synchronize_session=False
        )
        db.commit()

        c.post(
            "/agents/presence",
            json={"session_id": str(session.id), "state": "thinking", "cwd": "/tmp"},
            headers=_auth_headers(),
        )

        db.expire_all()
        updated = db.query(AgentSession).filter(AgentSession.id == session.id).first()
        assert updated.user_state == "active", (
            f"thinking should auto-resume snoozed session, got user_state={updated.user_state!r}"
        )
        db.close()
    api_app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# 4. Backend: VALID_STATES constant is the source of truth
# ---------------------------------------------------------------------------


def test_valid_states_includes_new_states():
    """VALID_STATES in presence.py must include needs_user and blocked."""
    from zerg.routers.presence import VALID_STATES

    assert "needs_user" in VALID_STATES, "needs_user missing from VALID_STATES"
    assert "blocked" in VALID_STATES, "blocked missing from VALID_STATES"
    # Existing states must remain
    assert "thinking" in VALID_STATES
    assert "running" in VALID_STATES
    assert "idle" in VALID_STATES
