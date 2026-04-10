from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.routers import auth as auth_router
from zerg.routers import auth_browser
from zerg.routers import auth_gmail
from zerg.routers import auth_sso
from zerg.routers import timeline as timeline_router


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
        "/auth/google/gmail/start",
        "/auth/google/gmail",
    } <= aggregate_paths


def test_timeline_router_exposes_browser_archive_routes():
    timeline_paths = _route_paths(timeline_router.router)

    assert {
        "/timeline/sessions",
        "/timeline/sessions/summary",

        "/timeline/sessions/{session_id}",
        "/timeline/sessions/{session_id}/thread",
        "/timeline/sessions/{session_id}/workspace",
        "/timeline/sessions/{session_id}/events",
        "/timeline/sessions/{session_id}/preview",
        "/timeline/sessions/{session_id}/action",
        "/timeline/sessions/{session_id}/loop-mode",
        "/timeline/filters",
        "/timeline/recall",
        "/timeline/demo",
    } <= timeline_paths



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
