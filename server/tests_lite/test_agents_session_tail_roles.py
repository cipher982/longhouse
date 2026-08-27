"""HTTP-level tests for the ``roles`` filter on GET /api/agents/sessions/{id}/tail.

Why this exists: a tool-heavy transcript is mostly tool output, so an unfiltered
tail of the last N events can be almost entirely command spam with the decisions
scrolled out of reach. ``roles`` must filter *before* the limit so a caller asking
for N turns gets N turns.

The route reads the storage-v2 projection, so the tool-heavy transcript is
shipped the way the Machine Agent ships one -- a real envelope into a real
catalog -- rather than seeded as archive rows.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents.store import AgentsStore

_TS = datetime(2026, 7, 21, 18, 0, 0, tzinfo=timezone.utc)
DEVICE_ID = "cinder"
BURIED_TURN = "the tablet is already paired for wireless adb"


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


def _owner_headers(live_catalog) -> dict[str, str]:
    owner_id = live_catalog.create_user("owner@tail-roles.test")
    return {"X-Agents-Token": live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)}


def _ship_tool_heavy_session(live_catalog, client, headers, *, conversation_reset: bool = False):
    """Ship a session shaped like the g55 incident: one turn buried in tool spam.

    One real turn first, then 60 tool records. An unfiltered tail of 10 cannot
    see the turn; a roles-filtered tail of 10 must.
    """
    session_id = uuid4()
    texts = (BURIED_TURN,) + tuple(f"Script completed\nWall time 0.{index} seconds" for index in range(60))
    if conversation_reset:
        texts += ("Conversation reset",)
    body = live_catalog.envelope_body(session_id=session_id, device_id=DEVICE_ID, texts=texts)
    records = body["render"]["records"]
    for record in records[1:]:
        record["role"] = "tool"
        record["tool_name"] = "Bash"
    if conversation_reset:
        records[-1]["role"] = "system"
        records[-1]["tool_name"] = None
        records[-1]["branch_kind"] = "conversation_reset"

    response = client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={**headers, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    return session_id


def test_tail_without_roles_returns_tool_spam(live_catalog, live_catalog_client):
    headers = _owner_headers(live_catalog)
    session_id = _ship_tool_heavy_session(live_catalog, live_catalog_client, headers)

    resp = live_catalog_client.get(f"/agents/sessions/{session_id}/tail", params={"limit": 10}, headers=headers)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert {event["role"] for event in payload["events"]} == {"tool"}
    assert payload["roles"] == ["assistant", "tool", "user"]


def test_tail_roles_filter_surfaces_buried_turns(live_catalog, live_catalog_client):
    headers = _owner_headers(live_catalog)
    session_id = _ship_tool_heavy_session(live_catalog, live_catalog_client, headers)

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        params={"limit": 10, "roles": "user,assistant"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["roles"] == ["assistant", "user"]
    assert [event["role"] for event in payload["events"]] == ["user"]
    assert "wireless adb" in payload["events"][0]["content"]
    # The narrowed scan reached past every tool record without filling, so the
    # short result means there are genuinely no more turns.
    assert payload["window_exhausted"] is False


def test_tail_roles_filter_reaches_past_the_limit_window(live_catalog, live_catalog_client):
    """The filter must apply before the limit, not after.

    A post-limit filter would take the last 10 events (all tool) and then drop
    the non-matching ones, returning nothing. This is the assertion that fails
    if the ordering ever regresses.
    """
    headers = _owner_headers(live_catalog)
    session_id = _ship_tool_heavy_session(live_catalog, live_catalog_client, headers)

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        params={"limit": 3, "roles": "user"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["total"] == 1, "the buried user turn must survive the limit window"
    assert payload["events"][0]["role"] == "user"


def test_session_preview_skips_provider_controls_before_limit(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    session_id = uuid4()
    try:
        with factory() as db:
            session = AgentSession(id=session_id, provider="claude", environment="test", started_at=_TS)
            db.add(session)
            db.flush()
            db.add_all(
                [
                    AgentEvent(
                        session_id=session_id,
                        role="user",
                        content_text="provider-local control without raw evidence",
                        title_eligible=0,
                        raw_json=None,
                        timestamp=_TS - timedelta(seconds=1),
                    ),
                    AgentEvent(
                        session_id=session_id,
                        role="user",
                        content_text="<command-name>/effort</command-name>",
                        raw_json='{"type":"user","isMeta":true,"message":{"role":"user","content":"<command-name>/effort</command-name>"}}',
                        timestamp=_TS,
                    ),
                    AgentEvent(
                        session_id=session_id,
                        role="user",
                        content_text="real prompt",
                        raw_json='{"message":{"content":"real prompt"}}',
                        timestamp=_TS + timedelta(seconds=1),
                    ),
                    AgentEvent(
                        session_id=session_id,
                        role="assistant",
                        content_text="real response",
                        raw_json='{"message":{"content":"real response"}}',
                        timestamp=_TS + timedelta(seconds=2),
                    ),
                ]
            )
            db.commit()
            preview = AgentsStore(db).get_session_preview(session_id, 2)
            first_user = AgentsStore(db).get_first_message_map([session_id], role="user")
            from zerg.services.session_summaries import events_to_dicts

            summary_events = events_to_dicts(
                db.query(AgentEvent)
                .filter(AgentEvent.session_id == session_id)
                .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
                .all(),
                provider="claude",
            )

        assert [(event.role, event.content_text) for event in preview] == [
            ("user", "real prompt"),
            ("assistant", "real response"),
        ]
        assert first_user == {session_id: "real prompt"}
        assert [event["content_text"] for event in summary_events if event["role"] == "user"] == ["real prompt"]
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

    The window arithmetic needs hundreds of events at exact positions, so these
    stand in for catalogd rather than shipping a transcript that size.
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


def test_tail_rejects_unknown_role(live_catalog, live_catalog_client):
    """Role validation happens before the session lookup, so no transcript is needed."""
    headers = _owner_headers(live_catalog)

    resp = live_catalog_client.get(
        f"/agents/sessions/{uuid4()}/tail",
        params={"roles": "user,narrator"},
        headers=headers,
    )

    assert resp.status_code == 400, resp.text
    assert "narrator" in resp.json()["detail"]


def test_tail_system_role_surfaces_conversation_boundaries(live_catalog, live_catalog_client):
    headers = _owner_headers(live_catalog)
    session_id = _ship_tool_heavy_session(live_catalog, live_catalog_client, headers, conversation_reset=True)

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        params={"roles": "system"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["role"] == "system"
    assert events[0]["content"] == "Conversation reset"


def test_tail_blank_roles_falls_back_to_all(live_catalog, live_catalog_client):
    headers = _owner_headers(live_catalog)
    session_id = _ship_tool_heavy_session(live_catalog, live_catalog_client, headers)

    resp = live_catalog_client.get(f"/agents/sessions/{session_id}/tail", params={"roles": " "}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["roles"] == ["assistant", "tool", "user"]


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
