from __future__ import annotations

from contextlib import contextmanager
from inspect import signature
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.routers import auth as auth_router
from zerg.routers import auth_browser
from zerg.routers import auth_sso
from zerg.routers import timeline as timeline_router


def _route_paths(router) -> set[str]:
    return {route.path for route in router.routes}


def test_auth_router_aggregates_browser_and_sso_routes():
    aggregate_paths = _route_paths(auth_router.router)

    assert _route_paths(auth_browser.router) <= aggregate_paths
    assert _route_paths(auth_sso.router) <= aggregate_paths
    assert {
        "/auth/status",
        "/auth/verify",
        "/auth/password",
        "/auth/accept-handoff",
        "/auth/accept-native-handoff",
    } <= aggregate_paths


def test_timeline_router_exposes_browser_archive_routes():
    timeline_paths = _route_paths(timeline_router.router)

    assert {
        "/timeline/sessions",
        "/timeline/sessions/summary",

        "/timeline/sessions/{session_id}",
        "/timeline/sessions/{session_id}/thread",
        "/timeline/sessions/{session_id}/turns",
        "/timeline/sessions/{session_id}/turns/{turn_id}",
        "/timeline/sessions/{session_id}/workspace",
        "/timeline/sessions/{session_id}/events",
        "/timeline/sessions/{session_id}/preview",
        "/timeline/sessions/{session_id}/action",
        "/timeline/sessions/{session_id}/loop-mode",
        "/timeline/filters",
        "/timeline/recall",
        "/timeline/demo",
    } <= timeline_paths


def test_timeline_stream_routes_are_on_short_lived_auth_router():
    timeline_paths = _route_paths(timeline_router.router)
    stream_paths = _route_paths(timeline_router.timeline_stream_router)

    assert {
        "/timeline/sessions/stream",
        "/timeline/sessions/{session_id}/workspace/stream",
    } <= stream_paths
    assert "/timeline/sessions/stream" not in timeline_paths
    assert "/timeline/sessions/{session_id}/workspace/stream" not in timeline_paths


def test_timeline_stream_endpoints_do_not_request_db_dependency():
    stream_params = signature(timeline_router.stream_timeline_sessions).parameters
    workspace_params = signature(timeline_router.stream_session_workspace).parameters
    canary_params = signature(timeline_router.stream_canary_workspace).parameters

    assert "db" not in stream_params
    assert "db" not in workspace_params
    assert "db" not in canary_params


def test_short_lived_browser_auth_closes_db_after_validation():
    request = SimpleNamespace(cookies={"longhouse_session": "token"}, headers={})
    db = SimpleNamespace(closed=False)

    @contextmanager
    def fake_db_session():
        yield db
        db.closed = True

    with (
        patch("zerg.dependencies.browser_auth.db_session", fake_db_session),
        patch("zerg.dependencies.browser_auth._get_browser_session_user", return_value=object()) as auth,
    ):
        result = timeline_router.require_current_browser_user_short_lived(request)

    assert result is None
    assert db.closed is True
    auth.assert_called_once_with(request, db)



def test_get_current_browser_route_user_accepts_query_token_for_sse():
    request = SimpleNamespace(cookies={}, headers={})
    db = object()
    user = object()
    strategy = SimpleNamespace(validate_ws_token=lambda token, current_db: user)

    with (
        patch("zerg.dependencies.browser_route_auth._get_strategy", return_value=strategy),
        patch("zerg.dependencies.browser_route_auth.get_current_browser_user") as browser_user,
    ):
        result = get_current_browser_route_user(request, db=db, token="sse-token")

    assert result is user
    browser_user.assert_not_called()


def test_get_current_browser_route_user_rejects_bad_query_token():
    request = SimpleNamespace(cookies={}, headers={})
    db = object()
    strategy = SimpleNamespace(validate_ws_token=lambda token, current_db: None)

    with patch("zerg.dependencies.browser_route_auth._get_strategy", return_value=strategy):
        with pytest.raises(HTTPException) as exc_info:
            get_current_browser_route_user(request, db=db, token="bad-token")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"
