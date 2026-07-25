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

import pytest
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
        # This path filters in SQL across the whole session, so a short result
        # means there are genuinely no more turns, not an exhausted window.
        assert payload["scan_window"] is None
    finally:
        cleanup()


def test_tail_roles_filter_reaches_past_the_limit_window(tmp_path):
    """The filter must apply before the limit, not after.

    A post-limit filter would take the last 10 events (all tool) and then drop
    the non-matching ones, returning nothing. This is the assertion that fails
    if the ordering ever regresses.
    """
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_tool_heavy_session(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(
            f"/agents/sessions/{session_id}/tail",
            params={"limit": 3, "roles": "user"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] == 1, "the buried user turn must survive the limit window"
        assert payload["events"][0]["role"] == "user"
    finally:
        cleanup()


async def _storage_v2_tail(
    monkeypatch,
    *,
    event_count: int,
    limit: int,
    roles: str | None,
    contentless_every: int | None = None,
):
    """Drive the storage-v2 tail path with a synthetic projection.

    tests_lite runs on the legacy path, so the bounded-window behaviour has to be
    exercised by standing in for catalogd.
    """
    from zerg.routers import agents_sessions

    session_id = uuid4()

    def _event(index: int) -> dict:
        contentless = contentless_every is not None and index % contentless_every != 0
        return {
            "id": f"e{index}",
            "role": "user" if index % 10 == 0 else "tool",
            "content_text": None if contentless else f"event {index}",
            "tool_name": None,
            "timestamp": (_TS + timedelta(seconds=index)).isoformat(),
        }

    items = [{"kind": "event", "event": _event(index)} for index in range(event_count)]

    async def _fake_workspace(*, limit: int, **_kwargs):
        # catalogd returns at most `limit` newest events.
        return {"projection": {"items": items[-limit:]}}

    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "build_storage_v2_workspace", _fake_workspace)

    class _Auth:
        owner_id = 1

    return await agents_sessions.session_tail(
        session_id=session_id,
        limit=limit,
        roles=roles,
        db=None,
        _auth=_Auth(),
        _single=None,
    )


@pytest.mark.asyncio
async def test_tail_reports_exhaustion_even_on_a_full_page(monkeypatch):
    """A filled limit does not mean there is nothing older.

    Reporting exhaustion only on a short result would answer "no more" to a page
    that still hides history. Here the scan window saturates AND the limit fills.
    """
    # limit=2, roles=user -> scan_limit=50. 500 events, every 10th is a user turn,
    # so the newest 50 scanned contain 5 user turns: limit fills, window saturates.
    payload = await _storage_v2_tail(monkeypatch, event_count=500, limit=2, roles="user")

    assert payload["total"] == 2
    assert payload["scan_window"] == 50
    assert payload["window_exhausted"] is True, "a full page can still hide older turns"


@pytest.mark.asyncio
async def test_tail_not_exhausted_when_whole_session_fits_in_window(monkeypatch):
    payload = await _storage_v2_tail(monkeypatch, event_count=20, limit=5, roles="user")

    assert payload["scan_window"] == 20
    assert payload["window_exhausted"] is False


@pytest.mark.asyncio
async def test_tail_storage_v2_returns_newest_matches(monkeypatch):
    """events[-limit:] must keep the NEWEST matches, not the oldest."""
    payload = await _storage_v2_tail(monkeypatch, event_count=100, limit=2, roles="user")

    # User turns sit at indices 0,10,...,90. The newest two in a 50-event window
    # are events 80 and 90.
    assert [event["content"] for event in payload["events"]] == ["event 80", "event 90"]


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


@pytest.mark.asyncio
async def test_tail_exhaustion_survives_a_contentless_page(monkeypatch):
    """Exhaustion must key off the raw page, not what survived filtering.

    A full scan page whose rows mostly lack content would otherwise look like a
    short scan, and the response would claim nothing older exists while never
    having looked past the window.
    """
    # limit=10, all roles -> scan_limit=10. Only every 4th event carries content,
    # so the raw page is full at 10 while few rows survive the content filter.
    payload = await _storage_v2_tail(
        monkeypatch,
        event_count=500,
        limit=10,
        roles=None,
        contentless_every=4,
    )

    assert payload["scan_window"] == 10, "scan_window must count the raw page"
    assert payload["window_exhausted"] is True
    assert payload["total"] < 10, "content filtering thinned the page"
