"""Tests for CORS behavior on error responses.

These tests verify that SafeErrorResponseMiddleware correctly includes/omits
CORS headers based on the allowed origins configuration.
"""

from fastapi import FastAPI

from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware


def _patch_middleware_origins(app, new_origins):
    """Patch the SafeErrorResponseMiddleware's cors_origins in user_middleware.

    In Starlette, middleware is stored as Middleware objects in `user_middleware`
    until the first request builds the middleware stack. We need to:
    1. Patch the kwargs in user_middleware
    2. Reset the cached middleware_stack to force a rebuild
    """
    # Find and patch SafeErrorResponseMiddleware in user_middleware
    for mw in app.user_middleware:
        if mw.cls is SafeErrorResponseMiddleware:
            mw.kwargs["cors_origins"] = new_origins
            break

    # Force middleware stack rebuild on next request
    app.middleware_stack = None


def test_error_handler_sets_origin_when_allowed(unauthenticated_client_no_raise, monkeypatch):
    """Error responses should include CORS headers when origin is allowed."""
    import zerg.main as main

    app: FastAPI = main.app

    # Patch middleware origins BEFORE making request
    _patch_middleware_origins(app, ["http://allowed.test"])

    # Register a throw route to trigger the error handler
    @app.get("/boom-test")
    async def boom_test():  # noqa: D401
        raise RuntimeError("boom")

    client = unauthenticated_client_no_raise
    r = client.get("/boom-test", headers={"Origin": "http://allowed.test"})
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == "http://allowed.test"
    assert r.headers.get("vary") == "Origin"


def test_error_handler_omits_origin_when_disallowed(unauthenticated_client_no_raise, monkeypatch):
    """Error responses should NOT include CORS origin header when origin is disallowed."""
    import zerg.main as main

    app: FastAPI = main.app

    # Patch middleware origins BEFORE making request
    _patch_middleware_origins(app, ["http://allowed.test"])

    @app.get("/boom-test-2")
    async def boom_test_2():  # noqa: D401
        raise RuntimeError("boom2")

    client = unauthenticated_client_no_raise
    r = client.get("/boom-test-2", headers={"Origin": "http://evil.test"})
    assert r.status_code == 500
    # No CORS allow-origin header for disallowed origins
    assert r.headers.get("access-control-allow-origin") is None
    assert r.headers.get("vary") == "Origin"
