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
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from sqlalchemy.orm import Session as SqlSession

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from zerg.catalogd.schema import create_catalog_engine
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.catalogd_supervisor import catalogd_paths
from zerg.services.session_hot_cards import upsert_timeline_card_from_session


def _make_db(tmp_path, name="forum.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed(
    factory,
    user_state="active",
    provider_session_id=None,
    *,
    started_at=None,
    project=None,
    last_user_message_preview=None,
    last_assistant_message_preview=None,
):
    db = factory()
    session_started_at = started_at or datetime.now(timezone.utc)
    s = AgentSession(
        provider="claude",
        environment="production",
        started_at=session_started_at,
        ended_at=None,
        project=project,
        user_messages=2,
        assistant_messages=2,
        tool_calls=0,
        user_state=user_state,
        last_user_message_preview=last_user_message_preview,
        last_assistant_message_preview=last_assistant_message_preview,
    )
    db.add(s)
    db.flush()
    upsert_timeline_card_from_session(db, s)
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

    def override_verify_agents_token():
        return SimpleNamespace(device_id="forum-filtering", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app)


def _seed_live_rows(rows):
    """Write hot-lane rows straight into the live catalog catalogd is serving."""

    database_path, _socket_path = catalogd_paths()
    engine = create_catalog_engine(database_path)
    try:
        with SqlSession(engine) as db:
            db.add_all(rows)
            db.commit()
    finally:
        engine.dispose()


def _seed_live_snoozed_session(session_id):
    """Mirror one archived session into the live catalog as the user snoozed it."""

    now = datetime.now(timezone.utc)
    _seed_live_rows(
        [
            LiveSessionCatalog(
                session_id=session_id,
                provider="claude",
                environment="production",
                started_at=now,
                user_state="snoozed",
                created_at=now,
                updated_at=now,
            )
        ]
    )


def _get_live_user_state(session_id):
    """Read the user state catalogd owns, which is the one presence flips."""

    database_path, _socket_path = catalogd_paths()
    engine = create_catalog_engine(database_path)
    try:
        with SqlSession(engine) as db:
            row = db.get(LiveSessionCatalog, session_id)
            return row.user_state if row is not None else None
    finally:
        engine.dispose()


def _get_user_state(factory, session_id):
    db = factory()
    s = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    state = s.user_state if s else None
    db.close()
    return state


def _get_presence_row(factory, session_id):
    """Return (phase, active_tool) from the runtime-state reducer."""
    db = factory()
    row = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.session_id == session_id)
        .order_by(SessionRuntimeState.updated_at.desc())
        .first()
    )
    phase = row.phase if row else None
    active_tool = row.active_tool if row else None
    db.close()
    return phase, active_tool


# ---------------------------------------------------------------------------
# Forum list filtering
# ---------------------------------------------------------------------------


def test_presence_auto_resumes_snoozed_on_thinking(tmp_path, live_catalog):
    """Presence thinking signal auto-resumes a snoozed session."""
    factory = _make_db(tmp_path, "auto_resume.db")
    sid = _seed(factory, user_state="snoozed")
    _seed_live_snoozed_session(sid)

    client = _client(factory)
    try:
        resp = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "thinking", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert resp.status_code == 204
        assert _get_live_user_state(sid) == "active"
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_stale_presence_does_not_auto_resume_snoozed_session(tmp_path):
    """Older thinking after a newer blocked signal must not resume the session."""
    factory = _make_db(tmp_path, "stale_auto_resume.db")
    sid = _seed(factory, user_state="snoozed")
    now = datetime.now(timezone.utc)

    client = _client(factory)
    try:
        blocked = client.post(
            "/agents/presence",
            json={
                "session_id": sid,
                "state": "blocked",
                "tool_name": "Bash",
                "provider": "claude",
                "occurred_at": now.isoformat(),
                "dedupe_key": "blocked-new",
            },
            headers={"X-Device-Token": "dev"},
        )
        assert blocked.status_code == 204

        thinking = client.post(
            "/agents/presence",
            json={
                "session_id": sid,
                "state": "thinking",
                "provider": "claude",
                "occurred_at": (now - timedelta(seconds=30)).isoformat(),
                "dedupe_key": "thinking-old",
            },
            headers={"X-Device-Token": "dev"},
        )
        assert thinking.status_code == 204

        assert _get_user_state(factory, sid) == "snoozed"
        assert _get_presence_row(factory, sid) == ("blocked", "Bash")
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_presence_auto_resumes_snoozed_on_running(tmp_path, live_catalog):
    """Presence running signal auto-resumes a snoozed session."""
    factory = _make_db(tmp_path, "auto_resume_run.db")
    sid = _seed(factory, user_state="snoozed")
    _seed_live_snoozed_session(sid)

    client = _client(factory)
    try:
        resp = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "running", "tool_name": "bash", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert resp.status_code == 204
        assert _get_live_user_state(sid) == "active"
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


def test_presence_invalid_state_is_noop(tmp_path):
    """Truly unknown presence states are ignored (no DB write, no state transitions)."""
    factory = _make_db(tmp_path, "invalid_presence_state.db")
    sid = _seed(factory, user_state="snoozed")

    client = _client(factory)
    try:
        resp = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "totally_unknown_state", "tool_name": "bash", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert resp.status_code == 204
        assert _get_user_state(factory, sid) == "snoozed"
        assert _get_presence_row(factory, sid) == (None, None)
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_presence_non_running_state_clears_tool_name(tmp_path):
    """tool_name is cleared whenever state transitions away from running."""
    factory = _make_db(tmp_path, "presence_tool_name_clear.db")
    sid = _seed(factory, user_state="active")

    client = _client(factory)
    try:
        running = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "running", "tool_name": "bash", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert running.status_code == 204
        assert _get_presence_row(factory, sid) == ("running", "bash")

        thinking = client.post(
            "/agents/presence",
            json={"session_id": sid, "state": "thinking", "tool_name": "read", "provider": "claude"},
            headers={"X-Device-Token": "dev"},
        )
        assert thinking.status_code == 204
        assert _get_presence_row(factory, sid) == ("thinking", None)
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()
