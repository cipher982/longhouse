"""Tests for POST /agents/demo (demo session seed endpoint)."""

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.services.demo_sessions import build_demo_agent_sessions


def _make_db(tmp_path, name="demo_seed.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(factory, *, provider_session_id: str | None):
    """Seed an AgentSession + primary SessionThread + SessionThreadAlias row.

    Post session-identity-kernel cleanup: ``provider_session_id`` is stored
    as a ``session_thread_aliases`` row, not a column on ``AgentSession``.
    """
    from uuid import uuid4

    from zerg.models.agents import SessionThread, SessionThreadAlias

    db = factory()
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="production",
        device_id="demo-mac",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.flush()

    if provider_session_id:
        thread = SessionThread(
            id=uuid4(),
            session_id=session.id,
            provider="claude",
            is_primary=1,
        )
        db.add(thread)
        db.flush()
        session.primary_thread_id = thread.id
        db.add(
            SessionThreadAlias(
                thread_id=thread.id,
                alias_kind="provider_session_id",
                alias_value=provider_session_id,
                provider="claude",
            )
        )

    db.commit()
    db.close()


def _client(factory):
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="demo-device", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app)


def _count_demo_sessions(factory) -> int:
    """Count demo sessions by their session_thread_aliases provider_session_id.

    Post session-identity-kernel cleanup: ``provider_session_id`` is not a
    column on ``AgentSession``, it is a SessionThreadAlias row.
    """
    from zerg.models.agents import SessionThread, SessionThreadAlias

    db = factory()
    try:
        return (
            db.query(SessionThread.session_id)
            .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
            .filter(SessionThreadAlias.alias_kind == "provider_session_id")
            .filter(SessionThreadAlias.alias_value.like("demo-%"))
            .distinct()
            .count()
        )
    finally:
        db.close()


def test_demo_seed_tops_up_missing_sessions(tmp_path):
    factory = _make_db(tmp_path, "demo_top_up.db")
    all_demo_sessions = build_demo_agent_sessions()
    assert all_demo_sessions

    # Pre-seed one demo session to simulate partial startup seed.
    _seed_session(factory, provider_session_id=all_demo_sessions[0].provider_session_id)
    assert _count_demo_sessions(factory) == 1

    client = _client(factory)
    try:
        resp = client.post("/agents/demo", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["seeded"] is True
        assert body["sessions_created"] == len(all_demo_sessions) - 1
        assert body["sessions_failed"] == 0
        assert body["sessions_deleted"] == 0
        assert _count_demo_sessions(factory) == len(all_demo_sessions)
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_demo_seed_is_idempotent_when_complete(tmp_path):
    factory = _make_db(tmp_path, "demo_idempotent.db")
    client = _client(factory)
    try:
        first = client.post("/agents/demo", headers={"X-Agents-Token": "dev"})
        assert first.status_code == 200
        assert first.json()["seeded"] is True

        second = client.post("/agents/demo", headers={"X-Agents-Token": "dev"})
        assert second.status_code == 200
        assert second.json()["seeded"] is False
        assert second.json()["sessions_created"] == 0
        assert second.json()["sessions_failed"] == 0
        assert second.json()["sessions_deleted"] == 0
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_demo_seed_replace_deletes_existing_demo_rows(tmp_path):
    factory = _make_db(tmp_path, "demo_replace.db")
    _seed_session(factory, provider_session_id="demo-stale-row")

    client = _client(factory)
    try:
        resp = client.post("/agents/demo?replace=true", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["seeded"] is True
        assert body["sessions_deleted"] == 1
        assert body["sessions_failed"] == 0
        assert body["sessions_created"] == len(build_demo_agent_sessions())
        assert _count_demo_sessions(factory) == len(build_demo_agent_sessions())
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_demo_seed_replace_blocked_when_auth_enabled(tmp_path):
    factory = _make_db(tmp_path, "demo_replace_auth.db")
    client = _client(factory)
    try:
        with patch("zerg.routers.agents_demo.get_settings") as mock_settings:
            mock_settings.return_value.auth_disabled = False
            mock_settings.return_value.testing = True
            resp = client.post("/agents/demo?replace=true", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 403
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()
