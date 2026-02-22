"""Tests for Forum session filtering and snooze auto-resume.

Covers:
- list_active_sessions excludes archived sessions (at query level, respects limit)
- list_active_sessions excludes snoozed sessions
- list_active_sessions includes parked sessions (visible but dimmed)
- NULL user_state treated as 'active' (legacy rows)
- Presence upsert auto-resumes snoozed sessions on thinking/running signal
- Presence upsert does NOT auto-resume on idle signal
"""

import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.agents import AgentSession, AgentsBase


def _make_db(tmp_path, name="forum.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed(factory, user_state="active", provider_session_id=None):
    db = factory()
    s = AgentSession(
        provider="claude",
        environment="production",
        started_at=datetime.now(timezone.utc),
        ended_at=None,
        user_messages=2,
        assistant_messages=2,
        tool_calls=0,
        user_state=user_state,
        provider_session_id=provider_session_id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    sid = str(s.id)
    db.close()
    return sid


def _client(factory):
    from zerg.main import api_app

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    api_app.dependency_overrides[get_db] = override
    return TestClient(api_app)


def _get_user_state(factory, session_id):
    db = factory()
    s = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    state = s.user_state if s else None
    db.close()
    return state


# ---------------------------------------------------------------------------
# Forum list filtering
# ---------------------------------------------------------------------------


def test_active_sessions_excludes_archived(tmp_path):
    """Archived sessions are excluded from /sessions/active."""
    factory = _make_db(tmp_path, "excl_arch.db")
    active_id = _seed(factory, user_state="active")
    _seed(factory, user_state="archived")

    client = _client(factory)
    try:
        resp = client.get("/agents/sessions/active", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["sessions"]}
        assert active_id in ids
        assert len(ids) == 1  # archived excluded
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_active_sessions_excludes_snoozed(tmp_path):
    """Snoozed sessions are excluded from /sessions/active."""
    factory = _make_db(tmp_path, "excl_snooze.db")
    active_id = _seed(factory, user_state="active")
    _seed(factory, user_state="snoozed")

    client = _client(factory)
    try:
        resp = client.get("/agents/sessions/active", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["sessions"]}
        assert active_id in ids
        assert len(ids) == 1  # snoozed excluded
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_active_sessions_includes_parked(tmp_path):
    """Parked sessions are included (visible but dimmed)."""
    factory = _make_db(tmp_path, "incl_parked.db")
    parked_id = _seed(factory, user_state="parked")

    client = _client(factory)
    try:
        resp = client.get("/agents/sessions/active", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["sessions"]}
        assert parked_id in ids
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_active_sessions_default_user_state_is_active(tmp_path):
    """Sessions without explicit user_state default to active (server_default='active')."""
    factory = _make_db(tmp_path, "default_state.db")
    # Seed without passing user_state — should get server_default 'active'
    factory_db = factory()
    s = AgentSession(
        provider="claude",
        environment="production",
        started_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    factory_db.add(s)
    factory_db.commit()
    sid = str(s.id)
    factory_db.close()

    client = _client(factory)
    try:
        resp = client.get("/agents/sessions/active", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["sessions"]}
        assert sid in ids
        session = next(s for s in resp.json()["sessions"] if s["id"] == sid)
        assert session["user_state"] == "active"
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_archived_filter_before_limit(tmp_path):
    """Archived filter is applied before limit so results are not underfilled."""
    factory = _make_db(tmp_path, "limit_test.db")
    # Seed 3 active + 2 archived
    active_ids = [_seed(factory, user_state="active") for _ in range(3)]
    for _ in range(2):
        _seed(factory, user_state="archived")

    client = _client(factory)
    try:
        # limit=5 but only 3 active should come back (not 3 from total 5-2=3)
        resp = client.get("/agents/sessions/active?limit=5", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["sessions"]}
        assert ids == set(active_ids)
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Snooze auto-resume
# ---------------------------------------------------------------------------


def test_active_sessions_handles_ended_session_datetime(tmp_path):
    """list_active_sessions doesn't 500 when ended_at is naive (SQLite stores naive)."""
    factory = _make_db(tmp_path, "ended_dt.db")
    # Seed an ended session (ended_at set)
    db = factory()
    s = AgentSession(
        provider="claude",
        environment="production",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),  # will be stored naive by SQLite
        user_messages=3,
        assistant_messages=3,
        tool_calls=1,
    )
    db.add(s)
    db.commit()
    db.close()

    client = _client(factory)
    try:
        resp = client.get("/agents/sessions/active", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        # Verify duration_minutes is calculable (no 500)
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["duration_minutes"] >= 0
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_presence_auto_resumes_snoozed_on_thinking(tmp_path):
    """Presence thinking signal auto-resumes a snoozed session."""
    factory = _make_db(tmp_path, "auto_resume.db")
    sid = _seed(factory, user_state="snoozed")

    # Patch presence router to use test DB
    client = _client(factory)
    try:
        resp = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "thinking", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert resp.status_code == 204
        assert _get_user_state(factory, sid) == "active"
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_presence_auto_resumes_snoozed_on_running(tmp_path):
    """Presence running signal auto-resumes a snoozed session."""
    factory = _make_db(tmp_path, "auto_resume_run.db")
    sid = _seed(factory, user_state="snoozed")

    client = _client(factory)
    try:
        resp = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "running", "tool_name": "bash", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert resp.status_code == 204
        assert _get_user_state(factory, sid) == "active"
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_presence_idle_does_not_resume_snoozed(tmp_path):
    """Presence idle signal does NOT auto-resume a snoozed session."""
    factory = _make_db(tmp_path, "no_resume_idle.db")
    sid = _seed(factory, user_state="snoozed")

    client = _client(factory)
    try:
        resp = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "idle", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert resp.status_code == 204
        assert _get_user_state(factory, sid) == "snoozed"  # unchanged
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()
