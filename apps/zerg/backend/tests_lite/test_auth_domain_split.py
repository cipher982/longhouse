from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from zerg.dependencies import auth as auth_deps
from zerg.dependencies import browser_auth
from zerg.dependencies.agents_auth import verify_agents_read_access
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.routers import auth as auth_router
from zerg.routers import auth_browser
from zerg.routers import auth_gmail
from zerg.routers import auth_sso


def _route_paths(router) -> set[str]:
    return {route.path for route in router.routes}


def test_auth_router_aggregates_browser_sso_and_gmail_routes():
    aggregate_paths = _route_paths(auth_router.router)

    assert _route_paths(auth_browser.router) <= aggregate_paths
    assert _route_paths(auth_sso.router) <= aggregate_paths
    assert _route_paths(auth_gmail.router) <= aggregate_paths
    assert {
        "/auth/status",
        "/auth/verify",
        "/auth/password",
        "/auth/accept-token",
        "/auth/sso",
        "/auth/google/gmail/start",
        "/auth/google/gmail",
    } <= aggregate_paths


def test_dependencies_auth_reexports_browser_session_dependencies():
    assert auth_deps.get_current_browser_user is browser_auth.get_current_browser_user
    assert auth_deps.get_optional_browser_user is browser_auth.get_optional_browser_user


def test_verify_agents_read_access_uses_browser_cookie_before_device_token():
    db = object()
    request = SimpleNamespace(
        cookies={"longhouse_session": "session-cookie"},
        headers={},
        state=SimpleNamespace(),
    )

    with (
        patch("zerg.dependencies.agents_auth._settings", SimpleNamespace(auth_disabled=False)),
        patch("zerg.dependencies.agents_auth.get_current_browser_user", return_value=object()) as browser_user,
        patch("zerg.dependencies.agents_auth.verify_agents_token") as verify_token,
    ):
        verify_agents_read_access(request, db=db)

    browser_user.assert_called_once_with(request, db)
    verify_token.assert_not_called()


def test_verify_agents_read_access_falls_back_to_device_token_after_bad_cookie():
    db = object()
    request = SimpleNamespace(
        cookies={"longhouse_session": "bad-cookie"},
        headers={"X-Agents-Token": "zdt_test"},
        state=SimpleNamespace(),
    )

    with (
        patch("zerg.dependencies.agents_auth._settings", SimpleNamespace(auth_disabled=False)),
        patch(
            "zerg.dependencies.agents_auth.get_current_browser_user",
            side_effect=HTTPException(status_code=401, detail="bad cookie"),
        ) as browser_user,
        patch("zerg.dependencies.agents_auth.verify_agents_token", return_value=None) as verify_token,
    ):
        verify_agents_read_access(request, db=db)

    browser_user.assert_called_once_with(request, db)
    verify_token.assert_called_once_with(request, db)


def test_get_current_oikos_user_accepts_query_token_for_sse():
    request = SimpleNamespace(cookies={}, headers={})
    db = object()
    user = object()
    strategy = SimpleNamespace(validate_ws_token=lambda token, current_db: user)

    with (
        patch("zerg.dependencies.oikos_auth._get_strategy", return_value=strategy),
        patch("zerg.dependencies.oikos_auth.get_current_browser_user") as browser_user,
    ):
        result = get_current_oikos_user(request, db=db, token="sse-token")

    assert result is user
    browser_user.assert_not_called()


def test_get_current_oikos_user_rejects_bad_query_token():
    request = SimpleNamespace(cookies={}, headers={})
    db = object()
    strategy = SimpleNamespace(validate_ws_token=lambda token, current_db: None)

    with patch("zerg.dependencies.oikos_auth._get_strategy", return_value=strategy):
        with pytest.raises(HTTPException) as exc_info:
            get_current_oikos_user(request, db=db, token="bad-token")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"
