"""Stage-6 authentication test-suite.

This module adds the missing coverage for:

* 6.1 – ID-token exchange & JWT minting.
* 6.2 – Auth-guard blocks unauthenticated access when ``AUTH_DISABLED`` is **off**.
* 6.3 – Dev-mode bypass works when ``AUTH_DISABLED`` is **on**.

All existing tests assume the bypass flag is *on* by default so we need to
*temporarily* patch :pydata:`zerg.dependencies.auth.AUTH_DISABLED` inside each
test case to simulate the desired mode.  Using ``monkeypatch`` ensures the
original value is restored afterwards and therefore does **not** interfere
with the rest of the suite.
"""

from __future__ import annotations

import time
from typing import Any
from typing import Dict

from fastapi.testclient import TestClient

from zerg.crud import crud
from zerg.dependencies import auth as auth_dep
from zerg.routers import auth as auth_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_jwt(token: str) -> Dict[str, Any]:
    """Decode *HS256* JWT without external deps (uses project fallback)."""

    return auth_dep._decode_jwt_fallback(token, auth_dep.JWT_SECRET)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 6.1 – Google ID-token → platform JWT
# ---------------------------------------------------------------------------


def test_google_login_exchanges_token(monkeypatch, client: TestClient, db_session):
    """POST /api/auth/google should create a user row and return a signed JWT."""

    # ------------------------------------------------------------------
    # 1. Force *prod* mode (AUTH_DISABLED = False) for this test
    # ------------------------------------------------------------------
    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

    # ------------------------------------------------------------------
    # 2. Stub Google token verification so we don't hit external network
    # ------------------------------------------------------------------
    fake_claims = {"email": "alice@example.com", "sub": "google-123"}
    monkeypatch.setattr(auth_router, "_verify_google_id_token", lambda _tok: fake_claims)

    # ------------------------------------------------------------------
    # 3. Call the endpoint – body can contain any dummy string, it is ignored
    #    by the patched verifier.
    # ------------------------------------------------------------------
    resp = client.post("/api/auth/google", json={"id_token": "dummy"})
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert {"access_token", "expires_in"}.issubset(data.keys())

    jwt_str: str = data["access_token"]

    # ------------------------------------------------------------------
    # 4. JWT should be valid HS256 signed token with correct claims
    # ------------------------------------------------------------------
    payload = _decode_jwt(jwt_str)

    assert payload["email"] == fake_claims["email"]

    # ``sub`` should equal the new user.id (integer as string)
    user = crud.get_user_by_email(db_session, fake_claims["email"])
    assert user is not None
    assert str(user.id) == payload["sub"]

    # Expiry ~30 min in the future (allow 5 s tolerance for test runtime)
    now = time.time()
    assert payload["exp"] - now > 1700  # 30 min ‑ 5 s buffer


# ---------------------------------------------------------------------------
# 6.2 – Auth-guard blocks unauthenticated access when bypass disabled
# ---------------------------------------------------------------------------


def test_auth_guard_requires_jwt(monkeypatch, client: TestClient):
    """With bypass **off** unauthenticated calls must return HTTP 401."""

    # Switch to production mode
    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

    resp = client.get("/api/agents")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 6.3 – AUTH_DISABLED bypass continues to work for local dev & legacy tests
# ---------------------------------------------------------------------------


def test_auth_guard_dev_bypass(monkeypatch, client: TestClient, db_session):
    """When bypass is *on* the same request should succeed as dev user."""

    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", True)

    resp = client.get("/api/agents")
    assert resp.status_code == 200

    # Ensure the dev user exists in DB
    dev_user = crud.get_user_by_email(db_session, auth_dep.DEV_EMAIL)
    assert dev_user is not None
