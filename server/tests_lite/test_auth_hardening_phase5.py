"""Auth hardening, asserted against a real Runtime Host.

``AUTH_DISABLED=1`` is the ``make dev`` shape, not a test shim, so the one
route test here provisions a real live catalog and then disables auth on top of
it. The old version proved a tokenless request reached a SQLAlchemy session
that production never opens; what actually has to hold is that the agents auth
dependency does not answer 401 when the operator turned auth off.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.main import _enforce_single_tenant_startup


def test_agents_routes_allow_missing_device_token_when_auth_disabled(monkeypatch):
    """Dev mode serves a machine read that carries no token at all."""

    with provision_live_catalog() as live:
        live.create_user("auth-disabled@test.local")
        # Only the environment: ``verify_agents_token`` reads settings per
        # request, which is what makes the operator's switch take effect.
        monkeypatch.setenv("AUTH_DISABLED", "1")
        with live.http_client() as client:
            response = client.get("/agents/sessions/wall")

        assert response.status_code == 200, response.text
        assert response.json()["total"] == 0


def test_single_tenant_config_requires_explicit_owner_email():
    from zerg.services.single_tenant import validate_single_tenant_config

    # No OWNER_EMAIL and no password auth configured → must fail closed.
    settings = SimpleNamespace(
        single_tenant=True,
        auth_disabled=False,
        admin_emails="admin@example.com",
        longhouse_password="",
        longhouse_password_hash="",
    )

    with (
        patch("zerg.services.single_tenant.get_settings", return_value=settings),
        patch.dict(os.environ, {}, clear=True),
    ):
        error = validate_single_tenant_config()

    assert error is not None
    assert "OWNER_EMAIL" in error


def test_single_tenant_config_allows_password_auth_without_owner_email():
    """B9: password-auth self-hosters may enable auth without OWNER_EMAIL."""
    from zerg.services.single_tenant import validate_single_tenant_config

    settings = SimpleNamespace(
        single_tenant=True,
        auth_disabled=False,
        admin_emails="",
        longhouse_password="",
        longhouse_password_hash="pbkdf2_sha256$600000$abc$def",
    )

    with (
        patch("zerg.services.single_tenant.get_settings", return_value=settings),
        patch.dict(os.environ, {}, clear=True),
    ):
        assert validate_single_tenant_config() is None


def test_single_tenant_startup_fails_fast_on_owner_misconfig():
    app = FastAPI()

    with (
        patch("zerg.lifespan._settings", SimpleNamespace(single_tenant=True, testing=False)),
        patch("zerg.services.single_tenant.validate_single_tenant_config", return_value="OWNER_EMAIL missing"),
    ):
        with pytest.raises(RuntimeError, match="OWNER_EMAIL missing"):
            _enforce_single_tenant_startup(app)

    assert app.state.single_tenant_violation == "OWNER_EMAIL missing"
