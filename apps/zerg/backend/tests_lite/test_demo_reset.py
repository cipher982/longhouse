"""Tests for DELETE /agents/demo (demo session reset endpoint).

Covers:
- Endpoint deletes sessions with device_id='demo-mac'
- Endpoint does NOT delete real sessions
- Endpoint returns 403 when AUTH_DISABLED is False
- Endpoint returns count of deleted sessions
"""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.agents import AgentSession, AgentsBase


def _make_db(tmp_path, name="demo_reset.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed(factory, *, device_id):
    db = factory()
    s = AgentSession(
        provider="claude",
        environment="production",
        device_id=device_id,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(s)
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


def _count(factory):
    db = factory()
    n = db.query(AgentSession).count()
    db.close()
    return n


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_demo_reset_deletes_demo_sessions(tmp_path):
    """DELETE /agents/demo removes sessions with device_id='demo-mac'."""
    factory = _make_db(tmp_path, "del_demo.db")
    _seed(factory, device_id="demo-mac")
    _seed(factory, device_id="demo-mac")
    assert _count(factory) == 2

    client = _client(factory)
    try:
        resp = client.delete("/agents/demo", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sessions_created"] == 2
        assert _count(factory) == 0
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_demo_reset_preserves_real_sessions(tmp_path):
    """DELETE /agents/demo does not touch sessions with other device_ids."""
    factory = _make_db(tmp_path, "preserve_real.db")
    _seed(factory, device_id="demo-mac")
    _seed(factory, device_id="laptop-abc")
    assert _count(factory) == 2

    client = _client(factory)
    try:
        resp = client.delete("/agents/demo", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        assert resp.json()["sessions_created"] == 1
        assert _count(factory) == 1
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_demo_reset_blocked_when_auth_enabled(tmp_path):
    """DELETE /agents/demo returns 403 when AUTH_DISABLED is False."""
    factory = _make_db(tmp_path, "auth_block.db")

    client = _client(factory)
    try:
        with patch("zerg.routers.agents.get_settings") as mock_settings:
            mock_settings.return_value.auth_disabled = False
            mock_settings.return_value.testing = True
            resp = client.delete("/agents/demo", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 403
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()


def test_demo_reset_no_sessions_returns_zero(tmp_path):
    """DELETE /agents/demo on empty DB returns 0 deleted."""
    factory = _make_db(tmp_path, "empty_reset.db")

    client = _client(factory)
    try:
        resp = client.delete("/agents/demo", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        assert resp.json()["sessions_created"] == 0
    finally:
        from zerg.main import api_app
        api_app.dependency_overrides.clear()
