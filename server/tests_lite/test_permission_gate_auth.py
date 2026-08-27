"""Auth-enabled tests for the permission-gate endpoints.

The live catalog is what turns authentication on here: the harness shapes the
process the way a Runtime Host shapes it (no ``TESTING``, no ``AUTH_DISABLED``,
a file-backed live database with catalogd in front of it), so
``verify_agents_token`` really validates the hook token and the session-scoped
enforcement really runs. A hook-scoped managed-session token must match its
session, and a machine-wide durable device token must be rejected — it cannot
be scoped to one session. This is the security boundary that keeps one managed
session from registering/polling/resolving another session's permission
requests.

Registration is a catalogd write against a session catalogd knows, so the
accepted case is seeded the way production seeds it: a managed local launch
registers the session, and the hook token is minted for that session.
"""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.auth.managed_session_tokens import MANAGED_SESSION_SCOPE_HOOK
from zerg.auth.managed_session_tokens import issue_managed_session_token

DEVICE_ID = "cinder"
OWNER_EMAIL = "perm-auth@example.test"


def _owner(live: LiveCatalog) -> int:
    return live.create_user(OWNER_EMAIL)


def _launch_managed_session(live: LiveCatalog, client: TestClient, owner_id: int) -> UUID:
    """Register a managed local session the way ``longhouse claude`` does."""

    session_id = uuid4()
    response = client.post(
        "/sessions/managed-local/this-device",
        json={
            "cwd": "/tmp/perm-auth",
            "provider": "claude",
            "project": "perm-auth",
            "session_id": str(session_id),
            "native_claude_channels_available": True,
        },
        headers={"X-Agents-Token": live.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)},
    )
    assert response.status_code == 200, response.text
    return session_id


def _hook_token(owner_id: int, session_id) -> str:
    return issue_managed_session_token(
        owner_id=owner_id,
        session_id=str(session_id),
        project="perm-auth",
        device_id=DEVICE_ID,
        scope=MANAGED_SESSION_SCOPE_HOOK,
    )


def _register(client: TestClient, session_id, token: str, tool_use_id: str = "toolu_auth"):
    return client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        headers={"X-Agents-Token": token},
    )


def test_session_scoped_hook_token_can_register(live_catalog, live_catalog_client):
    owner_id = _owner(live_catalog)
    session_id = _launch_managed_session(live_catalog, live_catalog_client, owner_id)

    resp = _register(live_catalog_client, session_id, _hook_token(owner_id, session_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["pause_request_id"]


def test_missing_token_is_401(live_catalog, live_catalog_client):
    owner_id = _owner(live_catalog)
    session_id = _launch_managed_session(live_catalog, live_catalog_client, owner_id)

    resp = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": "x", "tool_name": "Bash"},
    )

    assert resp.status_code == 401, resp.text


def test_durable_device_token_cannot_register(live_catalog, live_catalog_client):
    """A machine-wide token authorizes the machine, never one session."""

    owner_id = _owner(live_catalog)
    session_id = _launch_managed_session(live_catalog, live_catalog_client, owner_id)
    device_token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)

    resp = _register(live_catalog_client, session_id, device_token)

    assert resp.status_code == 403, resp.text


def test_hook_token_for_other_session_is_403_on_register(live_catalog, live_catalog_client):
    owner_id = _owner(live_catalog)
    session_a = _launch_managed_session(live_catalog, live_catalog_client, owner_id)
    session_b = _launch_managed_session(live_catalog, live_catalog_client, owner_id)

    # Token bound to session A cannot register against session B, even though B
    # exists and belongs to the same owner.
    resp = _register(live_catalog_client, session_b, _hook_token(owner_id, session_a))

    assert resp.status_code == 403, resp.text


def test_hook_token_for_other_session_is_403_on_poll(live_catalog, live_catalog_client):
    owner_id = _owner(live_catalog)
    session_a = _launch_managed_session(live_catalog, live_catalog_client, owner_id)
    session_b = _launch_managed_session(live_catalog, live_catalog_client, owner_id)

    resp = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_b), "tool_use_id": "x"},
        headers={"X-Agents-Token": _hook_token(owner_id, session_a)},
    )

    assert resp.status_code == 403, resp.text
