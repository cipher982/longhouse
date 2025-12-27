"""Tests for SafeErrorResponseMiddleware.

This middleware ensures HTTP protocol correctness when handling exceptions:
- Exceptions BEFORE response start: emit JSON 500 with CORS headers
- Exceptions AFTER response start (transport): log at DEBUG, don't send
- Exceptions AFTER response start (app bug): log at ERROR, don't send
"""

from __future__ import annotations

import json
import logging

import pytest


@pytest.mark.asyncio
async def test_exception_before_response_start_returns_json_500() -> None:
    """If exception occurs before http.response.start, send JSON 500."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def failing_app(scope, receive, send):
        raise ValueError("Something went wrong")

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(failing_app, cors_origins=["http://localhost:3000"])
    scope = {
        "type": "http",
        "headers": [(b"origin", b"http://localhost:3000")],
        "method": "GET",
        "path": "/test",
    }

    await middleware(scope, receive, send)

    # Should have sent http.response.start and http.response.body
    assert len(messages) == 2
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 500
    assert messages[1]["type"] == "http.response.body"

    # Body should be JSON error
    body = json.loads(messages[1]["body"])
    assert body == {"detail": "Internal server error"}


@pytest.mark.asyncio
async def test_exception_before_response_includes_cors_headers() -> None:
    """CORS headers should be included when origin matches."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def failing_app(scope, receive, send):
        raise ValueError("boom")

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(
        failing_app, cors_origins=["http://localhost:3000", "http://example.com"]
    )
    scope = {
        "type": "http",
        "headers": [(b"origin", b"http://example.com")],
        "method": "GET",
        "path": "/",
    }

    await middleware(scope, receive, send)

    # Check CORS headers in response
    headers = dict(messages[0]["headers"])
    assert headers[b"access-control-allow-origin"] == b"http://example.com"
    assert headers[b"access-control-allow-credentials"] == b"true"


@pytest.mark.asyncio
async def test_exception_with_non_matching_origin_no_cors() -> None:
    """If origin doesn't match, don't include CORS headers."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def failing_app(scope, receive, send):
        raise ValueError("boom")

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(failing_app, cors_origins=["http://allowed.com"])
    scope = {
        "type": "http",
        "headers": [(b"origin", b"http://evil.com")],
        "method": "GET",
        "path": "/",
    }

    await middleware(scope, receive, send)

    headers = dict(messages[0]["headers"])
    assert b"access-control-allow-origin" not in headers


@pytest.mark.asyncio
async def test_transport_exception_after_response_start_logs_debug(caplog) -> None:
    """Transport exceptions after response start should log at DEBUG, not ERROR."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def app_that_fails_after_start(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # Simulate transport-layer failure (client disconnected)
        raise BrokenPipeError("Connection reset by peer")

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(app_that_fails_after_start)
    scope = {"type": "http", "headers": [], "method": "GET", "path": "/"}

    with caplog.at_level(logging.DEBUG, logger="zerg.middleware.safe_error_response"):
        await middleware(scope, receive, send)

    # Should only have the original http.response.start, NOT an error response
    assert len(messages) == 1
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200

    # Should have logged at DEBUG, not ERROR
    debug_logs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    error_logs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(debug_logs) >= 1
    assert "Transport exception" in debug_logs[0].message
    assert len(error_logs) == 0


@pytest.mark.asyncio
async def test_app_exception_after_response_start_logs_error(caplog) -> None:
    """App exceptions after response start should log at ERROR (but still not send)."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def app_that_fails_after_start(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # Simulate app bug during streaming
        raise RuntimeError("Unexpected app error during streaming")

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(app_that_fails_after_start)
    scope = {"type": "http", "headers": [], "method": "GET", "path": "/"}

    with caplog.at_level(logging.DEBUG, logger="zerg.middleware.safe_error_response"):
        await middleware(scope, receive, send)

    # Should only have the original http.response.start, NOT an error response
    assert len(messages) == 1
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200

    # Should have logged at ERROR (this is an app bug, not transport churn)
    error_logs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_logs) >= 1
    assert "cannot send error response" in error_logs[0].message


@pytest.mark.asyncio
async def test_successful_request_passes_through() -> None:
    """Normal requests should pass through unmodified."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def successful_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK"})

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(successful_app)
    scope = {"type": "http", "headers": [], "method": "GET", "path": "/"}

    await middleware(scope, receive, send)

    assert len(messages) == 2
    assert messages[0]["status"] == 200
    assert messages[1]["body"] == b"OK"


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    """Non-HTTP scopes (websocket, lifespan) should pass through."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    app_called = False

    async def inner_app(scope, receive, send):
        nonlocal app_called
        app_called = True

    middleware = SafeErrorResponseMiddleware(inner_app)
    scope = {"type": "websocket"}

    await middleware(scope, lambda: None, lambda x: None)

    assert app_called


@pytest.mark.asyncio
async def test_wildcard_cors_origin() -> None:
    """Wildcard '*' in cors_origins should match any origin."""
    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def failing_app(scope, receive, send):
        raise ValueError("boom")

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(failing_app, cors_origins=["*"])
    scope = {
        "type": "http",
        "headers": [(b"origin", b"http://any-origin.com")],
        "method": "GET",
        "path": "/",
    }

    await middleware(scope, receive, send)

    headers = dict(messages[0]["headers"])
    # Note: Even with "*", we echo the specific origin (required for credentials)
    assert headers[b"access-control-allow-origin"] == b"http://any-origin.com"


@pytest.mark.asyncio
async def test_h11_local_protocol_error_treated_as_transport() -> None:
    """h11.LocalProtocolError should be treated as transport, not app error."""
    from h11._util import LocalProtocolError

    from zerg.middleware.safe_error_response import SafeErrorResponseMiddleware

    async def app_that_fails_after_start(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise LocalProtocolError("Too much data for declared Content-Length")

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = SafeErrorResponseMiddleware(app_that_fails_after_start)
    scope = {"type": "http", "headers": [], "method": "GET", "path": "/"}

    # Should NOT raise
    await middleware(scope, receive, send)

    # Should only have the original response start
    assert len(messages) == 1
    assert messages[0]["status"] == 200
