"""Tests for the structured per-session loop mode endpoint."""

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.services.session_hot_cards import upsert_timeline_card_from_session


def _make_db(tmp_path, name="loop_mode.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(factory, *, loop_mode="assist"):
    db = factory()
    session = AgentSession(
        provider="claude",
        environment="production",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=2,
        assistant_messages=2,
        tool_calls=0,
        loop_mode=loop_mode,
    )
    db.add(session)
    db.flush()
    upsert_timeline_card_from_session(db, session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()
    return session_id


def _client(factory):
    from zerg.main import api_app

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="session-loop-mode", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app)


def _get_loop_mode(factory, session_id):
    db = factory()
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    loop_mode = session.loop_mode if session else None
    db.close()
    return loop_mode


def test_get_session_exposes_loop_mode(tmp_path):
    factory = _make_db(tmp_path, "get_session_loop_mode.db")
    session_id = _seed_session(factory, loop_mode="assist")
    client = _client(factory)

    try:
        response = client.get(f"/agents/sessions/{session_id}", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 200
        assert response.json()["loop_mode"] == "assist"
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_patch_session_loop_mode_updates_value(tmp_path):
    factory = _make_db(tmp_path, "patch_loop_mode.db")
    session_id = _seed_session(factory)
    client = _client(factory)

    try:
        response = client.patch(
            f"/agents/sessions/{session_id}/loop-mode",
            json={"loop_mode": "autopilot"},
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 200
        assert response.json() == {"session_id": session_id, "loop_mode": "autopilot"}
        assert _get_loop_mode(factory, session_id) == "autopilot"
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_patch_session_timeline_visibility_hides_and_restores(tmp_path):
    factory = _make_db(tmp_path, "timeline_visibility.db")
    session_id = _seed_session(factory)
    client = _client(factory)

    try:
        hidden = client.patch(
            f"/agents/sessions/{session_id}/timeline-visibility",
            json={"hidden": True},
            headers={"X-Agents-Token": "dev"},
        )
        assert hidden.status_code == 200
        assert hidden.json() == {"session_id": session_id, "hidden": True}

        listing = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert listing.status_code == 200
        assert session_id not in {item["id"] for item in listing.json()["sessions"]}

        restored = client.patch(
            f"/agents/sessions/{session_id}/timeline-visibility",
            json={"hidden": False},
            headers={"X-Agents-Token": "dev"},
        )
        assert restored.status_code == 200
        assert restored.json() == {"session_id": session_id, "hidden": False}

        listing = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert session_id in {item["id"] for item in listing.json()["sessions"]}
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_active_sessions_exposes_loop_mode(tmp_path):
    factory = _make_db(tmp_path, "active_sessions_loop_mode.db")
    _seed_session(factory, loop_mode="autopilot")
    client = _client(factory)

    try:
        response = client.get("/agents/sessions/active", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["sessions"]
        assert payload["sessions"][0]["loop_mode"] == "autopilot"
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_invalid_loop_mode_rejected(tmp_path):
    factory = _make_db(tmp_path, "invalid_loop_mode.db")
    session_id = _seed_session(factory)
    client = _client(factory)

    try:
        response = client.patch(
            f"/agents/sessions/{session_id}/loop-mode",
            json={"loop_mode": "wild-west"},
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 422
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_legacy_manual_loop_mode_reads_as_assist(tmp_path):
    factory = _make_db(tmp_path, "legacy_manual_loop_mode.db")
    session_id = _seed_session(factory, loop_mode="manual")
    client = _client(factory)

    try:
        response = client.get(f"/agents/sessions/{session_id}", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 200
        assert response.json()["loop_mode"] == "assist"
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()
