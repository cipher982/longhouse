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
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.session_loop_mode import SessionLoopMode

# ---------------------------------------------------------------------------
# DB + client fixtures (same pattern as other tests_lite tests)
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name="test.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
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

    def override_verify_agents_token():
        return SimpleNamespace(device_id="presence-fixture", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    with TestClient(api_app) as c:
        yield c
    api_app.dependency_overrides.clear()
    engine.dispose()


def _auth_headers() -> dict:
    return {"X-Agents-Token": "test-token"}


def _runtime_state(SessionLocal, sid: str) -> SessionRuntimeState | None:
    with SessionLocal() as db:
        return (
            db.query(SessionRuntimeState)
            .filter(SessionRuntimeState.session_id == sid)
            .order_by(SessionRuntimeState.updated_at.desc())
            .first()
        )


def _make_session(
    db,
    sid: str | None = None,
    *,
    summary: str | None = None,
    loop_mode: SessionLoopMode = SessionLoopMode.ASSIST,
) -> AgentSession:
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
        summary=summary,
        loop_mode=loop_mode.value,
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


def test_presence_releases_request_db_before_serialized_write(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/presence_release.db", pool_size=1, max_overflow=0)
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    observations: dict[str, int] = {}

    class _Serializer:
        is_configured = True

        async def execute_after_closing_request_session(self, fn, fallback_db, **_kwargs):
            observations["before_close"] = engine.pool.checkedout()
            fallback_db.close()
            observations["after_close"] = engine.pool.checkedout()
            with SessionLocal() as write_db:
                result = fn(write_db)
                write_db.commit()
                return result

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("presence must release the request DB before waiting on serialized writes")

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="presence-release", id="token-1", owner_id=1)

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr("zerg.routers.presence.get_write_serializer", lambda: _Serializer())
    monkeypatch.setattr("zerg.database.get_session_factory", lambda: SessionLocal)
    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    try:
        with TestClient(api_app) as c:
            response = c.post(
                "/agents/presence",
                json={
                    "session_id": str(uuid4()),
                    "state": "idle",
                    "provider": "claude",
                    "dedupe_key": "presence-release-1",
                },
                headers=_auth_headers(),
            )
        assert response.status_code == 204, response.text
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    assert observations == {"before_close": 0, "after_close": 0}


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
        state = _runtime_state(SessionLocal, sid)
        assert state is not None
        assert state.active_tool == "Bash", f"expected active_tool='Bash', got {state.active_tool!r}"
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
        state = _runtime_state(SessionLocal, sid)
        assert state is not None
        assert state.active_tool is None, f"expected active_tool=None on needs_user, got {state.active_tool!r}"
    api_app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# 3. Backend: auto-resume behaviour
#
# Auto-resume is a live-catalog fact. A Runtime Host hands the presence signal
# to catalogd, which is where the snoozed row lives and where the resume
# happens; these three tests therefore run against a real catalog instead of an
# archive session that production never opens.
# ---------------------------------------------------------------------------


def _snoozed_console_session(live_catalog: LiveCatalog, *, owner_id: int) -> str:
    """One Console session the user has snoozed, in the store that owns that fact."""

    now = datetime.now(timezone.utc)
    session_id = uuid4()
    created = live_catalog.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(uuid4()),
                "owner_id": owner_id,
                "provider": "claude",
                "device_id": "presence-fixture",
                "cwd": "/tmp",
                "started_at": now.isoformat(),
            }
        },
    )
    assert created["created"] is True, created
    snoozed = live_catalog.rpc(
        "session.preferences.update.v2",
        {
            "session_id": str(session_id),
            "owner_id": owner_id,
            "user_state": "snoozed",
            "loop_mode": None,
            "notification_muted": None,
            "user_hidden_from_timeline": None,
            "last_read_at": None,
            "observed_at": now.isoformat(),
        },
    )
    assert snoozed["found"] is True, snoozed
    return str(session_id)


def _user_state(live_catalog: LiveCatalog, session_id: str) -> str:
    return str(live_catalog.rpc("session.read.v2", {"session_id": session_id})["facts"]["catalog"]["user_state"])


def _post_presence(live_catalog: LiveCatalog, client, *, owner_id: int, session_id: str, state: str) -> None:
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="presence-fixture")
    response = client.post(
        "/agents/presence",
        json={"session_id": session_id, "state": state, "cwd": "/tmp", "provider": "claude"},
        headers={"X-Agents-Token": token},
    )
    assert response.status_code == 204, response.text


def test_needs_user_does_not_auto_resume_snoozed(live_catalog, live_catalog_client):
    """needs_user must NOT auto-resume a snoozed session."""
    owner_id = live_catalog.create_user("owner@presence.test")
    session_id = _snoozed_console_session(live_catalog, owner_id=owner_id)

    _post_presence(live_catalog, live_catalog_client, owner_id=owner_id, session_id=session_id, state="needs_user")

    user_state = _user_state(live_catalog, session_id)
    assert user_state == "snoozed", f"needs_user should NOT auto-resume snoozed session, got user_state={user_state!r}"


def test_blocked_does_not_auto_resume_snoozed(live_catalog, live_catalog_client):
    """blocked must NOT auto-resume a snoozed session."""
    owner_id = live_catalog.create_user("owner@presence.test")
    session_id = _snoozed_console_session(live_catalog, owner_id=owner_id)

    _post_presence(live_catalog, live_catalog_client, owner_id=owner_id, session_id=session_id, state="blocked")

    user_state = _user_state(live_catalog, session_id)
    assert user_state == "snoozed", f"blocked should NOT auto-resume snoozed session, got user_state={user_state!r}"


def test_thinking_still_auto_resumes_snoozed(live_catalog, live_catalog_client):
    """thinking still auto-resumes snoozed sessions (existing behaviour must not regress)."""
    owner_id = live_catalog.create_user("owner@presence.test")
    session_id = _snoozed_console_session(live_catalog, owner_id=owner_id)

    _post_presence(live_catalog, live_catalog_client, owner_id=owner_id, session_id=session_id, state="thinking")

    user_state = _user_state(live_catalog, session_id)
    assert user_state == "active", f"thinking should auto-resume snoozed session, got user_state={user_state!r}"


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
        state = _runtime_state(SessionLocal, sid)
        assert state is not None
        assert state.active_tool == "Bash", (
            f"active_tool should be preserved after Notification/permission_prompt, got {state.active_tool!r}"
        )
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
        state = _runtime_state(SessionLocal, sid)
        assert state is not None
        assert state.active_tool == "Bash", f"active_tool should be set by PermissionRequest, got {state.active_tool!r}"
    api_app.dependency_overrides.clear()
    engine.dispose()


# Legacy assistant-subsystem tests removed after subsystem deletion
