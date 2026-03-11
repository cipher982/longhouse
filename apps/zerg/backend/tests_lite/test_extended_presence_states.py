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
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import api_app
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.user import User
from zerg.routers.agents import verify_agents_token

# ---------------------------------------------------------------------------
# DB + client fixtures (same pattern as other tests_lite tests)
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name="test.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
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


def test_blocked_permission_request_then_notification_preserves_tool_name(client, tmp_path):
    """PermissionRequest (blocked+tool) followed by Notification/permission_prompt (blocked, no tool)
    must NOT clobber the tool_name set by the first event."""
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
        # PermissionRequest fires first — carries tool_name
        c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "tool_name": "Bash", "cwd": "/tmp"},
            headers=_auth_headers(),
        )
        # Notification/permission_prompt fires second — no tool_name
        c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "cwd": "/tmp"},
            headers=_auth_headers(),
        )
        db = SessionLocal()
        row = db.query(SessionPresence).filter(SessionPresence.session_id == sid).first()
        assert row is not None
        assert row.tool_name == "Bash", (
            f"tool_name should be preserved after Notification/permission_prompt, got {row.tool_name!r}"
        )
        db.close()
    api_app.dependency_overrides.clear()
    engine.dispose()


def test_blocked_notification_then_permission_request_sets_tool_name(client, tmp_path):
    """Notification/permission_prompt (no tool) then PermissionRequest (with tool) — tool name wins."""
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
        # Notification fires first — no tool_name
        c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "cwd": "/tmp"},
            headers=_auth_headers(),
        )
        # PermissionRequest fires second — carries tool_name
        c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "tool_name": "Bash", "cwd": "/tmp"},
            headers=_auth_headers(),
        )
        db = SessionLocal()
        row = db.query(SessionPresence).filter(SessionPresence.session_id == sid).first()
        assert row is not None
        assert row.tool_name == "Bash", (
            f"tool_name should be set by PermissionRequest, got {row.tool_name!r}"
        )
        db.close()
    api_app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# 5. Backend: operator-mode wakeups for actionable pause states
# ---------------------------------------------------------------------------


def test_blocked_wakes_operator_once_when_enabled(monkeypatch, tmp_path):
    """blocked transitions wake proactive Oikos when operator mode is enabled."""
    engine, SessionLocal = _make_db(tmp_path, "operator_wakeup.db")
    calls = []

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(owner_id=42)

    async def fake_invoke_oikos(owner_id, message, message_id, **kwargs):
        calls.append(
            {
                "owner_id": owner_id,
                "message": message,
                "message_id": message_id,
                **kwargs,
            }
        )
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.routers.presence.invoke_oikos", fake_invoke_oikos)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    with TestClient(api_app) as c:
        sid = str(uuid4())
        response = c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "tool_name": "Bash", "cwd": "/tmp/test"},
            headers=_auth_headers(),
        )
        assert response.status_code == 204

    api_app.dependency_overrides.clear()
    engine.dispose()

    assert len(calls) == 1
    assert calls[0]["owner_id"] == 42
    assert calls[0]["source"] == "operator"
    assert f"Session ID: {sid}" in calls[0]["message"]
    assert "Trigger: presence.blocked" in calls[0]["message"]
    assert "Tool: Bash" in calls[0]["message"]
    assert calls[0]["surface_adapter"].surface_id == "operator"
    assert calls[0]["surface_payload"]["session_id"] == sid
    assert calls[0]["surface_payload"]["trigger_type"] == "presence.blocked"


def test_repeated_blocked_state_does_not_rewake_operator(monkeypatch, tmp_path):
    """The same blocked state should not wake operator mode twice without a real transition."""
    engine, SessionLocal = _make_db(tmp_path, "operator_dedupe.db")
    calls = []

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(owner_id=42)

    async def fake_invoke_oikos(owner_id, message, message_id, **kwargs):
        calls.append((owner_id, message, message_id, kwargs))
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.routers.presence.invoke_oikos", fake_invoke_oikos)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    with TestClient(api_app) as c:
        sid = str(uuid4())
        first = c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "tool_name": "Bash", "cwd": "/tmp/test"},
            headers=_auth_headers(),
        )
        second = c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "cwd": "/tmp/test"},
            headers=_auth_headers(),
        )
        assert first.status_code == 204
        assert second.status_code == 204

    api_app.dependency_overrides.clear()
    engine.dispose()

    assert len(calls) == 1


def test_needs_user_does_not_wake_operator_when_disabled(monkeypatch, tmp_path):
    """Operator wakeups stay dormant until explicitly enabled."""
    engine, SessionLocal = _make_db(tmp_path, "operator_disabled.db")
    calls = []

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    async def fake_invoke_oikos(owner_id, message, message_id, **kwargs):
        calls.append((owner_id, message, message_id, kwargs))
        return 123

    monkeypatch.delenv("OIKOS_OPERATOR_MODE_ENABLED", raising=False)
    monkeypatch.setattr("zerg.routers.presence.invoke_oikos", fake_invoke_oikos)

    api_app.dependency_overrides[get_db] = override_db
    with TestClient(api_app) as c:
        sid = str(uuid4())
        response = c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "needs_user", "cwd": "/tmp/test"},
            headers=_auth_headers(),
        )
        assert response.status_code == 204

    api_app.dependency_overrides.clear()
    engine.dispose()

    assert calls == []


def test_blocked_does_not_wake_operator_when_user_policy_disables_it(monkeypatch, tmp_path):
    """User-backed operator prefs can disable wakeups even when the env master switch is on."""
    engine, SessionLocal = _make_db(tmp_path, "operator_policy_disabled.db")
    calls = []

    with SessionLocal() as db:
        db.add(
            User(
                id=42,
                email="owner@test.local",
                role="ADMIN",
                context={"preferences": {"operator_mode": {"enabled": False}}},
            )
        )
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(owner_id=42)

    async def fake_invoke_oikos(owner_id, message, message_id, **kwargs):
        calls.append((owner_id, message, message_id, kwargs))
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.routers.presence.invoke_oikos", fake_invoke_oikos)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    with TestClient(api_app) as c:
        sid = str(uuid4())
        response = c.post(
            "/agents/presence",
            json={"session_id": sid, "state": "blocked", "tool_name": "Bash", "cwd": "/tmp/test"},
            headers=_auth_headers(),
        )
        assert response.status_code == 204

    api_app.dependency_overrides.clear()
    engine.dispose()

    assert calls == []
