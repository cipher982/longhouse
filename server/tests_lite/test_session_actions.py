"""Tests for POST /agents/sessions/{id}/action (Park/Snooze/Archive/Resume).

``user_state`` is user-owned session preference, and it lives in the live
catalog. The route writes it through ``session.preferences.update.v2``; there is
no archive row it touches and no SQLAlchemy session it opens. So each test here
stands up a real catalog, seeds one Console session in it, drives the HTTP route
with a real device token, and reads the state back out of the catalog.

Covers:
- park: sets user_state=parked
- snooze: sets user_state=snoozed
- archive: sets user_state=archived
- resume: resets user_state=active from any bucket
- Invalid action returns 400
- Unknown session returns 404
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402,F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402,F401


def _seed_session(live: LiveCatalog, *, user_state: str = "active") -> str:
    """One Console session in the live catalog, in the requested bucket."""

    owner_id = live.create_user("owner@session-actions.test")
    session_id = uuid4()
    live.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(uuid4()),
                "owner_id": owner_id,
                "provider": "claude",
                "device_id": "session-actions",
                "cwd": "/workspace/longhouse",
                "project": "longhouse",
                "started_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    if user_state != "active":
        live.rpc(
            "session.preferences.update.v2",
            {
                "session_id": str(session_id),
                "user_state": user_state,
                "loop_mode": None,
                "notification_muted": None,
                "user_hidden_from_timeline": None,
                "last_read_at": None,
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
    return str(session_id)


def _device_token(live: LiveCatalog) -> str:
    owner_id = live.create_user("owner@session-actions.test")
    return live.create_device_token(owner_id=owner_id, device_id="session-actions")


def _catalog_user_state(live: LiveCatalog, session_id: str) -> str:
    facts = live.rpc("session.read.v2", {"session_id": session_id})
    return str(facts["facts"]["catalog"]["user_state"])


# ---------------------------------------------------------------------------
# Action endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected_state",
    [
        ("park", "parked"),
        ("snooze", "snoozed"),
        ("archive", "archived"),
    ],
)
def test_action_sets_user_state(live_catalog, live_catalog_client, action, expected_state):  # noqa: F811
    """Each action sets the correct user_state, durably, in the live catalog."""
    session_id = _seed_session(live_catalog)

    resp = live_catalog_client.post(
        f"/agents/sessions/{session_id}/action",
        json={"action": action},
        headers={"X-Agents-Token": _device_token(live_catalog)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_state"] == expected_state
    assert _catalog_user_state(live_catalog, session_id) == expected_state


def test_resume_resets_to_active(live_catalog, live_catalog_client):  # noqa: F811
    """resume action restores user_state=active from any bucket."""
    session_id = _seed_session(live_catalog, user_state="parked")

    resp = live_catalog_client.post(
        f"/agents/sessions/{session_id}/action",
        json={"action": "resume"},
        headers={"X-Agents-Token": _device_token(live_catalog)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_state"] == "active"
    assert _catalog_user_state(live_catalog, session_id) == "active"


def test_invalid_action_returns_400(live_catalog, live_catalog_client):  # noqa: F811
    """Unknown action returns 400 before anything reaches the catalog."""
    session_id = _seed_session(live_catalog)

    resp = live_catalog_client.post(
        f"/agents/sessions/{session_id}/action",
        json={"action": "explode"},
        headers={"X-Agents-Token": _device_token(live_catalog)},
    )

    assert resp.status_code == 400, resp.text
    assert _catalog_user_state(live_catalog, session_id) == "active"


def test_unknown_session_returns_404(live_catalog, live_catalog_client):  # noqa: F811
    """A session the catalog does not carry returns 404, not a silent write."""
    resp = live_catalog_client.post(
        f"/agents/sessions/{uuid4()}/action",
        json={"action": "park"},
        headers={"X-Agents-Token": _device_token(live_catalog)},
    )

    assert resp.status_code == 404, resp.text
