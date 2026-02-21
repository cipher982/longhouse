"""Tests for POST /agents/sessions/{id}/action (Park/Snooze/Archive/Resume).

Covers:
- park: sets user_state=parked
- snooze: sets user_state=snoozed
- archive: sets user_state=archived, session excluded from active list
- resume: resets user_state=active
- Invalid action returns 400
- Unknown session returns 404
"""

import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.agents import AgentSession, AgentsBase


def _make_db(tmp_path, name="actions.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(factory, *, user_state="active"):
    db = factory()
    s = AgentSession(
        provider="claude",
        environment="production",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=2,
        assistant_messages=2,
        tool_calls=0,
        user_state=user_state,
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
    return TestClient(api_app), factory


def _get_user_state(factory, session_id):
    db = factory()
    s = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    state = s.user_state if s else None
    db.close()
    return state


# ---------------------------------------------------------------------------
# Action endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action,expected_state", [
    ("park", "parked"),
    ("snooze", "snoozed"),
    ("archive", "archived"),
])
def test_action_sets_user_state(tmp_path, action, expected_state):
    """Each action sets the correct user_state."""
    factory = _make_db(tmp_path, f"action_{action}.db")
    sid = _seed_session(factory)
    client, _ = _client(factory)

    try:
        resp = client.post(
            f"/agents/sessions/{sid}/action",
            json={"action": action},
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200
        assert resp.json()["user_state"] == expected_state
        assert _get_user_state(factory, sid) == expected_state
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_resume_resets_to_active(tmp_path):
    """resume action restores user_state=active from any bucket."""
    factory = _make_db(tmp_path, "resume.db")
    sid = _seed_session(factory, user_state="parked")
    client, _ = _client(factory)

    try:
        resp = client.post(
            f"/agents/sessions/{sid}/action",
            json={"action": "resume"},
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200
        assert resp.json()["user_state"] == "active"
        assert _get_user_state(factory, sid) == "active"
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_invalid_action_returns_400(tmp_path):
    """Unknown action returns 400."""
    factory = _make_db(tmp_path, "invalid.db")
    sid = _seed_session(factory)
    client, _ = _client(factory)

    try:
        resp = client.post(
            f"/agents/sessions/{sid}/action",
            json={"action": "explode"},
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 400
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_unknown_session_returns_404(tmp_path):
    """Nonexistent session returns 404."""
    import uuid
    factory = _make_db(tmp_path, "notfound.db")
    client, _ = _client(factory)

    try:
        resp = client.post(
            f"/agents/sessions/{uuid.uuid4()}/action",
            json={"action": "park"},
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 404
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()
