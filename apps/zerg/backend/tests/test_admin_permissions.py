"""Permissions test for *admin* routes (Stage 9.3).

The `/api/admin/reset-database` endpoint is protected by the new
`require_admin` dependency.  We verify that

1. A normal *USER* receives HTTP 403.
2. An *ADMIN* succeeds (the actual DB reset is monkey-patched so the test
   does not alter the in-memory SQLite schema).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zerg.config import get_settings
from zerg.crud import crud
from zerg.dependencies import auth as auth_dep
from zerg.routers import auth as auth_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _google_login(client: TestClient, monkeypatch, email: str) -> str:
    """Return JWT by stubbing Google verification."""

    fake_claims = {"email": email, "sub": "google-test"}
    monkeypatch.setattr(auth_router, "_verify_google_id_token", lambda _tok: fake_claims)
    resp = client.post("/api/auth/google", json={"id_token": "dummy"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _dev_env(monkeypatch):
    """Force ENVIRONMENT=development inside this module."""

    monkeypatch.setenv("ENVIRONMENT", "development")


def test_admin_route_requires_admin(monkeypatch, client: TestClient):
    """A normal *USER* must receive 403 when hitting /admin/reset-database."""

    # Production mode (auth enabled)
    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

    token = _google_login(client, monkeypatch, "eve@example.com")

    resp = client.post(
        "/api/admin/reset-database",
        headers={"Authorization": f"Bearer {token}"},
        json={"confirmation_password": None},
    )

    assert resp.status_code == 403


def test_admin_route_requires_super_admin(monkeypatch, client: TestClient, db_session):
    """A regular *ADMIN* (not in ADMIN_EMAILS) must receive 403."""

    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

    # Create a patched settings object with testing=False and no admin emails
    settings = get_settings()
    settings.testing = False
    settings.auth_disabled = False  # Explicitly disable auth bypass
    settings.admin_emails = ""

    # Mock get_settings to return our patched version
    import zerg.dependencies.auth as auth_module

    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)

    token = _google_login(client, monkeypatch, "alice@example.com")

    user = crud.get_user_by_email(db_session, "alice@example.com")
    assert user is not None
    user.role = "ADMIN"  # type: ignore[attr-defined]
    db_session.commit()

    resp = client.post(
        "/api/admin/reset-database",
        headers={"Authorization": f"Bearer {token}"},
        json={"confirmation_password": None},
    )

    assert resp.status_code == 403
    assert "Super admin privileges required" in resp.json()["detail"]


def test_admin_route_allows_admin(monkeypatch, client: TestClient, db_session):
    """A *SUPER ADMIN* user should be allowed (returns 200)."""

    monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

    # Set alice as super admin in ADMIN_EMAILS
    monkeypatch.setenv("ADMIN_EMAILS", "alice@example.com")

    # Log in as alice and then promote her to ADMIN in the database.
    token = _google_login(client, monkeypatch, "alice@example.com")

    user = crud.get_user_by_email(db_session, "alice@example.com")
    assert user is not None
    user.role = "ADMIN"  # type: ignore[attr-defined]
    db_session.commit()

    # Monkey-patch the *dangerous* drop/recreate helpers so the test does not
    # wipe the in-memory SQLite schema used by the rest of the suite.
    monkeypatch.setattr("zerg.database.Base.metadata.drop_all", lambda *a, **k: None)
    monkeypatch.setattr("zerg.database.initialize_database", lambda: None)

    resp = client.post(
        "/api/admin/reset-database",
        headers={"Authorization": f"Bearer {token}"},
        json={"confirmation_password": None},
    )

    assert resp.status_code == 200
