"""Tests for POST /agents/demo (demo session seed endpoint)."""

import os
from datetime import datetime
from datetime import timezone

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.services.demo_sessions import build_demo_agent_sessions


def _make_db(tmp_path, name="demo_seed.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(factory, *, provider_session_id: str | None):
    db = factory()
    db.add(
        AgentSession(
            provider="claude",
            environment="production",
            device_id="demo-mac",
            provider_session_id=provider_session_id,
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
    )
    db.commit()
    db.close()


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


def _count_demo_sessions(factory) -> int:
    db = factory()
    try:
        return db.query(AgentSession).filter(AgentSession.provider_session_id.like("demo-%")).count()
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
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()
