"""Stage-8 – WebSocket authentication tests.

We cover three scenarios:

1. *Prod mode* (``AUTH_DISABLED = False``) – connection **without** token is
   rejected and closed with code **4401**.
2. Prod mode – connection **with** a valid JWT succeeds.
3. *Dev mode* (``AUTH_DISABLED = True``) – bypass remains functional, so a
   connection without token is accepted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from zerg.dependencies import auth as auth_dep
from zerg.routers import auth as auth_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_jwt_for_email(client: TestClient, monkeypatch, email: str = "bob@example.com") -> str:
    """Issue a JWT for *email* by stubbing Google verification."""

    fake_claims = {"email": email, "sub": "google-xyz"}
    monkeypatch.setattr(auth_router, "_verify_google_id_token", lambda _tok: fake_claims)
    resp = client.post("/api/auth/google", json={"id_token": "dummy"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ws_rejects_without_token(monkeypatch, client: TestClient):
    """When auth is enabled the endpoint must reject missing token (4401)."""

    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/ws") as _ws:  # type: ignore[func-returns-value]
            pass  # pragma: no cover – we expect disconnect before this line

    assert exc_info.value.code == 4401


def test_ws_accepts_with_valid_token(monkeypatch, client: TestClient):
    """Connection should succeed when a valid JWT is supplied."""

    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

    token = _get_jwt_for_email(client, monkeypatch)

    with client.websocket_connect(f"/api/ws?token={token}") as _ws:  # type: ignore[func-returns-value]
        pass  # Successful connect is sufficient


def test_ws_dev_bypass(monkeypatch, client: TestClient):
    """With AUTH_DISABLED the same request *without* token must succeed."""

    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", True)

    with client.websocket_connect("/api/ws") as _ws:  # type: ignore[func-returns-value]
        pass
