"""HTTP-level tests for the ``roles`` filter on GET /api/agents/sessions/{id}/tail.

Overrides dependencies on ``api_app`` (not ``app``) per the tests_lite convention.

Why this exists: a tool-heavy transcript is mostly tool output, so an unfiltered
tail of the last N events can be almost entirely command spam with the decisions
scrolled out of reach. ``roles`` must filter *before* the limit so a caller asking
for N turns gets N turns.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession

_TS = datetime(2026, 7, 21, 18, 0, 0, tzinfo=timezone.utc)


def _setup_app(tmp_path):
    db_path = tmp_path / "test_session_tail_roles.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def _override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: None
    api_app.dependency_overrides[require_single_tenant] = lambda: None

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(verify_agents_token, None)
        api_app.dependency_overrides.pop(require_single_tenant, None)

    return factory, _cleanup


def _add_tool_heavy_session(factory):
    """Build a session shaped like the g55 incident: turns buried in tool spam."""
    with factory() as db:
        sess = AgentSession(id=uuid4(), provider="codex", environment="test", started_at=_TS)
        db.add(sess)
        db.flush()
        # One real turn first, then 60 tool events. An unfiltered tail of 10
        # cannot see the turn; a roles-filtered tail of 10 must.
        db.add(
            AgentEvent(
                session_id=sess.id,
                role="user",
                content_text="the tablet is already paired for wireless adb",
                timestamp=_TS,
                raw_json='{"role":"user"}',
            )
        )
        for index in range(60):
            db.add(
                AgentEvent(
                    session_id=sess.id,
                    role="tool",
                    content_text=f"Script completed\nWall time 0.{index} seconds",
                    timestamp=_TS + timedelta(seconds=index + 1),
                    raw_json='{"role":"tool"}',
                )
            )
        db.commit()
        return sess.id


def test_tail_without_roles_returns_tool_spam(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_tool_heavy_session(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{session_id}/tail", params={"limit": 10})
        assert resp.status_code == 200
        payload = resp.json()
        assert {event["role"] for event in payload["events"]} == {"tool"}
        assert payload["roles"] == ["assistant", "tool", "user"]
    finally:
        cleanup()


def test_tail_roles_filter_surfaces_buried_turns(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_tool_heavy_session(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(
            f"/agents/sessions/{session_id}/tail",
            params={"limit": 10, "roles": "user,assistant"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["roles"] == ["assistant", "user"]
        assert [event["role"] for event in payload["events"]] == ["user"]
        assert "wireless adb" in payload["events"][0]["content"]
    finally:
        cleanup()


def test_tail_rejects_unknown_role(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_tool_heavy_session(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(
            f"/agents/sessions/{session_id}/tail",
            params={"roles": "user,narrator"},
        )
        assert resp.status_code == 400
        assert "narrator" in resp.json()["detail"]
    finally:
        cleanup()


def test_tail_blank_roles_falls_back_to_all(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_tool_heavy_session(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{session_id}/tail", params={"roles": " "})
        assert resp.status_code == 200
        assert resp.json()["roles"] == ["assistant", "tool", "user"]
    finally:
        cleanup()
