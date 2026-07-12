from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from zerg.auth import catalog_gateway
from zerg.auth.principal import AuthenticatedUser
from zerg.auth.strategy import DevAuthStrategy
from zerg.auth.strategy import JWTAuthStrategy
from zerg.catalogd.client import CatalogUnavailable
from zerg.dependencies import auth as auth_deps
from zerg.dependencies import browser_auth


def _request(*, bearer: str | None = None, cookie: str | None = None) -> Request:
    headers = []
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    if cookie:
        headers.append((b"cookie", f"longhouse_session={cookie}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/api/auth/status", "headers": headers})


def _principal() -> AuthenticatedUser:
    return AuthenticatedUser(id=7, email="owner@example.com", role="ADMIN", prefs={}, context={})


def test_production_auth_dependency_does_not_open_catalog_db(monkeypatch):
    dependency = auth_deps._no_auth_db()
    assert next(dependency) is None
    with pytest.raises(StopIteration):
        next(dependency)


def test_production_browser_stream_auth_does_not_open_catalog_db(monkeypatch):
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setattr(browser_auth, "get_settings", lambda: SimpleNamespace(testing=False, control_plane_url=None))
    monkeypatch.setattr(browser_auth.auth_deps, "AUTH_DISABLED", False)
    monkeypatch.setattr(
        browser_auth,
        "db_session",
        lambda: (_ for _ in ()).throw(AssertionError("production stream auth opened SQLAlchemy")),
    )
    monkeypatch.setattr(
        browser_auth.auth_deps,
        "_get_strategy",
        lambda: SimpleNamespace(validate_ws_token=lambda token, db=None: _principal()),
    )

    assert browser_auth.get_current_browser_user_id_short_lived(_request(cookie="valid")) == 7


def test_jwt_strategy_resolves_detached_principal_without_using_supplied_db(monkeypatch):
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setattr("zerg.auth.strategy.get_settings", lambda: SimpleNamespace(testing=False, jwt_secret="secret"))
    strategy = JWTAuthStrategy()
    monkeypatch.setattr(strategy, "_decode", lambda token: {"sub": "7"})
    monkeypatch.setattr(catalog_gateway, "resolve_user", lambda user_id, touch_last_login: _principal())
    exploding_db = SimpleNamespace(query=lambda *args: (_ for _ in ()).throw(AssertionError("ORM used")))

    user = strategy.get_current_user(_request(bearer="header.payload.signature"), exploding_db)

    assert isinstance(user, AuthenticatedUser)
    assert user.id == 7


def test_auth_disabled_production_resolves_local_principal_through_catalog(monkeypatch):
    strategy = DevAuthStrategy()
    strategy._settings = SimpleNamespace(dev_admin=True, single_tenant=False)  # noqa: SLF001
    calls = []
    monkeypatch.setattr(catalog_gateway, "resolve_local_user", lambda **kwargs: calls.append(kwargs) or _principal())

    user = strategy.get_current_user(_request())

    assert user.id == 7
    assert calls == [
        {
            "email": "dev@local",
            "provider": "dev",
            "provider_user_id": "test-user-1",
            "role": "ADMIN",
            "adopt_existing": False,
            "require_email_match": False,
            "max_users": None,
            "promote_role": True,
        }
    ]


def test_catalog_pause_maps_to_typed_503(monkeypatch):
    monkeypatch.setattr(catalog_gateway, "catalogd_paths", lambda: (None, "/tmp/catalog.sock"))
    monkeypatch.setattr(
        catalog_gateway,
        "call_catalogd_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(CatalogUnavailable("paused")),
    )

    with pytest.raises(HTTPException) as exc:
        catalog_gateway.resolve_user(7, touch_last_login=False)

    assert exc.value.status_code == 503
    assert exc.value.detail == {
        "code": "catalog_unavailable",
        "message": "Catalog authentication is temporarily unavailable.",
    }
