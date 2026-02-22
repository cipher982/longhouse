"""Unit tests for usage-stats endpoint (live query against sessions table)."""
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from zerg.main import api_app
from zerg.database import get_db
from zerg.models.agents import AgentsBase, AgentSession


def _make_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    AgentsBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _add_session(db, provider, user_msgs=1, asst_msgs=1, tool_calls=0, started_days_ago=0):
    now = datetime.now(timezone.utc)
    s = AgentSession(
        provider=provider,
        environment="production",
        started_at=now - timedelta(days=started_days_ago, hours=1),
        ended_at=now - timedelta(days=started_days_ago),
        user_messages=user_msgs,
        assistant_messages=asst_msgs,
        tool_calls=tool_calls,
        needs_embedding=0,
    )
    db.add(s)
    db.commit()


def _client(factory):
    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override
    return TestClient(api_app)


def test_aggregates_by_provider(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    _add_session(db, "claude", user_msgs=2, asst_msgs=2, tool_calls=5)
    _add_session(db, "claude", user_msgs=1, asst_msgs=1)
    _add_session(db, "gemini", user_msgs=3, asst_msgs=3)

    resp = _client(factory).get("/agents/usage-stats")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_sessions"] == 3
    by_p = {r["provider"]: r for r in data["by_provider"]}
    assert by_p["claude"]["sessions"] == 2
    assert by_p["claude"]["messages"] == (2 + 2 + 5) + (1 + 1 + 0)
    assert by_p["gemini"]["sessions"] == 1


def test_multiple_providers_returned(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    _add_session(db, "claude")
    _add_session(db, "gemini")
    _add_session(db, "codex")

    resp = _client(factory).get("/agents/usage-stats")
    assert resp.status_code == 200
    providers = {r["provider"] for r in resp.json()["by_provider"]}
    assert providers == {"claude", "gemini", "codex"}


def test_days_param_filters_old_sessions(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    _add_session(db, "claude", started_days_ago=1)   # recent
    _add_session(db, "claude", started_days_ago=60)  # too old

    resp = _client(factory).get("/agents/usage-stats?days=30")
    assert resp.status_code == 200
    assert resp.json()["total_sessions"] == 1


def test_days_over_365_returns_422(tmp_path):
    factory = _make_db(tmp_path)
    resp = _client(factory).get("/agents/usage-stats?days=366")
    assert resp.status_code == 422


def test_empty_db_returns_zeros(tmp_path):
    factory = _make_db(tmp_path)
    resp = _client(factory).get("/agents/usage-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_sessions"] == 0
    assert data["total_messages"] == 0
    assert data["by_provider"] == []
