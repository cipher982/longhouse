"""Tests for has_real_sessions flag in the sessions list response.

Covers:
- has_real_sessions=False when all sessions have device_id='demo-mac'
- has_real_sessions=True when at least one session has a different device_id
- has_real_sessions=True when sessions have no device_id (None)
"""

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.agents import AgentSession, AgentsBase


def _make_db(tmp_path, name="test_real.db"):
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


def _get_client(factory):
    from fastapi.testclient import TestClient

    from zerg.main import api_app

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    api_app.dependency_overrides[get_db] = override
    client = TestClient(api_app)
    return client


def test_has_real_sessions_false_when_only_demo(tmp_path):
    """has_real_sessions=False when all sessions are demo (device_id='demo-mac')."""
    factory = _make_db(tmp_path, "demo_only.db")
    _seed(factory, device_id="demo-mac")
    _seed(factory, device_id="demo-mac")

    client = _get_client(factory)
    try:
        resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_real_sessions"] is False
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_has_real_sessions_true_when_real_session_exists(tmp_path):
    """has_real_sessions=True when at least one non-demo session exists."""
    factory = _make_db(tmp_path, "mixed.db")
    _seed(factory, device_id="demo-mac")
    _seed(factory, device_id="laptop-abc123")

    client = _get_client(factory)
    try:
        resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_real_sessions"] is True
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_has_real_sessions_true_when_no_device_id(tmp_path):
    """has_real_sessions=True when session has device_id=None (not a demo)."""
    factory = _make_db(tmp_path, "no_device.db")
    _seed(factory, device_id=None)

    client = _get_client(factory)
    try:
        resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_real_sessions"] is True
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()


def test_has_real_sessions_true_when_no_sessions(tmp_path):
    """has_real_sessions=True when there are no sessions (default, avoids false banners)."""
    factory = _make_db(tmp_path, "empty.db")

    client = _get_client(factory)
    try:
        resp = client.get("/agents/sessions", headers={"X-Agents-Token": "dev"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_real_sessions"] is True
    finally:
        from zerg.main import api_app

        api_app.dependency_overrides.clear()
