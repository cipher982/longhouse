"""Recent sorts by the owner's last composer input.

Spec: control-plane docs/specs/recent-by-last-user-input.md. Only the
browser-principal input routes stamp ``last_user_input_at``; it is a max-write
and is served on ``SessionStateFacts``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402,F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402,F401
from zerg.routers import session_chat  # noqa: E402
from zerg.routers.session_chat import SessionInputRequest  # noqa: E402

OWNER_EMAIL = "owner@owner-input-stamp.test"


def _console_session(live: LiveCatalog, *, owner_id: int) -> str:
    session_id = uuid4()
    created = live.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(uuid4()),
                "owner_id": owner_id,
                "provider": "codex",
                "device_id": "cinder",
                "cwd": "/workspace/longhouse",
                "project": "longhouse",
                "started_at": (datetime.now(UTC) - timedelta(days=3)).isoformat(),
            }
        },
    )
    assert created["created"] is True, created
    return str(session_id)


def _stamp(live: LiveCatalog, *, session_id: str, owner_id: int, at: datetime) -> dict:
    return live.rpc(
        "session.preferences.update.v2",
        {
            "session_id": session_id,
            "owner_id": owner_id,
            "user_state": None,
            "notification_muted": None,
            "user_hidden_from_timeline": None,
            "last_read_at": None,
            "last_user_input_at": at.isoformat(),
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )


def test_owner_input_is_a_max_write_served_on_session_state(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user(OWNER_EMAIL)
    session_id = _console_session(live_catalog, owner_id=owner)
    later = datetime.now(UTC).replace(microsecond=0)
    earlier = later - timedelta(hours=1)

    assert _stamp(live_catalog, session_id=session_id, owner_id=owner, at=later)["updated"] is True
    # An older stamp (a retried request, a skewed clock) never moves it back.
    assert _stamp(live_catalog, session_id=session_id, owner_id=owner, at=earlier)["updated"] is False
    # A stranger's write is the same "not found" an unknown session gets.
    assert _stamp(live_catalog, session_id=session_id, owner_id=owner + 1_000, at=later + timedelta(hours=1))["found"] is False

    catalog = live_catalog.rpc("session.read.v2", {"session_id": session_id})["facts"]["catalog"]
    assert datetime.fromisoformat(catalog["last_user_input_at"]) == later

    response = live_catalog_client.get(
        f"/timeline/sessions/{session_id}",
        cookies={"longhouse_session": live_catalog.browser_cookie(owner_id=owner, email=OWNER_EMAIL)},
    )
    assert response.status_code == 200, response.text
    served = response.json()["session_state"]["last_user_input_at"]
    assert datetime.fromisoformat(served.replace("Z", "+00:00")) == later


def test_reading_preferences_without_input_leaves_it_unknown(live_catalog):  # noqa: F811
    owner = live_catalog.create_user(OWNER_EMAIL)
    session_id = _console_session(live_catalog, owner_id=owner)
    live_catalog.rpc(
        "session.preferences.update.v2",
        {
            "session_id": session_id,
            "owner_id": owner,
            "user_state": "parked",
            "notification_muted": None,
            "user_hidden_from_timeline": None,
            "last_read_at": None,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
    catalog = live_catalog.rpc("session.read.v2", {"session_id": session_id})["facts"]["catalog"]
    assert catalog["last_user_input_at"] is None


def test_browser_input_route_stamps_and_machine_route_does_not(monkeypatch):
    stamped: list[tuple[str, int]] = []
    source_session = SimpleNamespace(id=uuid4())
    sentinel = object()

    async def fake_response(**_kwargs):
        return sentinel

    monkeypatch.setattr(session_chat, "_load_session_for_continuation", lambda *_a, **_k: source_session)
    monkeypatch.setattr(session_chat, "_create_session_input_response", fake_response)
    monkeypatch.setattr(
        session_chat,
        "stamp_owner_input_soon",
        lambda session_id, *, owner_id, client_request_id: stamped.append((session_id, owner_id, client_request_id)),
    )
    monkeypatch.setattr(session_chat, "_authorize_live_send", lambda **_k: None)
    monkeypatch.setattr(session_chat, "_resolve_agents_owner_id", lambda *_a: 7)
    body = SessionInputRequest(text="updates?", client_request_id="req-1")

    browser = asyncio.run(
        session_chat.create_session_input_endpoint(
            session_id=str(source_session.id),
            body=body,
            db=None,
            current_user=SimpleNamespace(id=7),
        )
    )
    assert browser is sentinel
    assert stamped == [(source_session.id, 7, "req-1")]

    machine = asyncio.run(
        session_chat.create_session_input_agents_endpoint(
            session_id=str(source_session.id),
            body=body,
            request=SimpleNamespace(),
            db=None,
            device_token=None,
            _single=None,
        )
    )
    assert machine is sentinel
    assert stamped == [(source_session.id, 7, "req-1")], "machine-route input must never count as the owner's"


def test_stamp_uses_the_receipt_time_and_skips_inputs_without_a_receipt(monkeypatch):
    """A rejected send has no receipt and stamps nothing; a replay finds the
    original receipt and re-stamps its original time, never "now"."""

    from zerg.services import live_session_inputs
    from zerg.services import session_preferences

    created_at = datetime(2026, 10, 7, 9, 17, tzinfo=UTC)
    receipts = {"accepted": SimpleNamespace(created_at=created_at)}
    writes: list[datetime] = []

    async def fake_load(*, owner_id, session_id, client_request_id):
        return receipts.get(client_request_id)

    async def fake_update(session_id, *, owner_id, last_user_input_at):
        writes.append(last_user_input_at)

    monkeypatch.setattr(live_session_inputs, "load_live_input_receipt_by_client_request", fake_load)
    monkeypatch.setattr(session_preferences, "update_session_preferences", fake_update)
    session_id = str(uuid4())

    asyncio.run(session_preferences.stamp_owner_input(session_id, owner_id=7, client_request_id="rejected"))
    assert writes == []
    asyncio.run(session_preferences.stamp_owner_input(session_id, owner_id=7, client_request_id="accepted"))
    asyncio.run(session_preferences.stamp_owner_input(session_id, owner_id=7, client_request_id="accepted"))
    assert writes == [created_at, created_at]
