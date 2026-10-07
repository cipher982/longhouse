"""Fence mutating HTTP requests during an authenticated cutover drain.

A raw ASGI middleware, not ``@app.middleware("http")``: Starlette's
BaseHTTPMiddleware re-sends every response as a streamed body, and
JSONCompressionMiddleware deliberately passes streamed bodies through, so while
this fence was a BaseHTTPMiddleware no JSON response left the origin gzipped.

An admitted request counts as in flight until it starts its response, the same
boundary BaseHTTPMiddleware's ``call_next`` gave: a long streamed body must not
hold a drain open.
"""

from __future__ import annotations

import asyncio
import json

from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_INTERNAL_CONTROL_PREFIXES = ("/internal/deployments/", "/api/internal/deployments/")
_RUNTIME_WEBSOCKET_PATHS = frozenset({"/api/agents/control/ws", "/api/runners/ws", "/api/ws"})
_REQUEST_BODY_DISCARD_TIMEOUT_SECONDS = 1.0
# The predecessor releases the catalog within about a second of its stop; a
# held request that outlives this bound gets a typed retryable 503 instead,
# well inside RequestTimeoutMiddleware's 15 s.
_CATALOG_HANDOFF_HOLD_SECONDS = 5.0


async def _discard_body(receive: Receive) -> bool:
    """Consume a rejected request body, but never let a stalled upload hang its 503."""
    try:
        async with asyncio.timeout(_REQUEST_BODY_DISCARD_TIMEOUT_SECONDS):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return False
                if message["type"] == "http.request" and not message.get("more_body", False):
                    return True
    except TimeoutError:
        return False


async def _deny_websocket(scope: Scope, send: Send, content: dict) -> None:
    response = JSONResponse(
        status_code=503,
        content=content,
        headers={"Retry-After": "2"},
    )
    if "websocket.http.response" not in scope.get("extensions", {}):
        await send({"type": "websocket.close", "code": 1013, "reason": json.dumps(content, separators=(",", ":"))})
        return
    await send(
        {
            "type": "websocket.http.response.start",
            "status": response.status_code,
            "headers": response.raw_headers,
        }
    )
    await send({"type": "websocket.http.response.body", "body": response.body})


async def _await_catalog_handoff() -> bool:
    """Hold a request that reached a warm candidate until its first reopen.

    Until the catalog is open there is nothing to serve. Between the catalog
    opening and reopen the deployer's readiness, probe and reopen are the
    critical path; released early, the reconnect backlog (stream replays,
    machine agents) held the candidate's event loop for 0.3-0.45 s on every
    canary cutover. Returns False when the bounded wait ends first; the caller
    then answers the same typed, retryable 503 as a closed runtime.
    """
    from zerg.services.catalog_handoff import catalog_handoff
    from zerg.services.runtime_admission import runtime_admission

    handoff = catalog_handoff()
    if handoff is None:
        return True
    runtime = runtime_admission()
    if runtime.initially_opened:
        return True
    if handoff.failed is not None:
        return False
    try:
        async with asyncio.timeout(_CATALOG_HANDOFF_HOLD_SECONDS):
            await handoff.ready.wait()
            await runtime.wait_until_initial_open()
    except TimeoutError:
        pass
    return runtime.initially_opened


def _restarting_payload(path: str) -> dict:
    from zerg.services.runtime_admission import runtime_admission

    return runtime_admission().restarting_payload(path=path)


def _wants_event_stream(scope: Scope) -> bool:
    if scope.get("method") != "GET":
        return False
    # Only the API's stream routes; every other path routes (and authenticates)
    # normally. The answer carries only the lifecycle claim that every K1
    # rejection already carries.
    path = scope.get("path", "")
    if not path.startswith("/api/") or not path.rstrip("/").endswith(("/stream", "-stream")):
        return False
    for name, value in scope.get("headers") or ():
        if name == b"accept" and b"text/event-stream" in value:
            return True
    return False


async def _end_stream_with_lifecycle(scope: Scope, receive: Receive, send: Send) -> bool:
    """A stream opened on a draining process gets the lifecycle and ends at once.

    Streams open at drain completion are ended the same way. One opened later
    (a client reconnecting before the edge moves) would otherwise stay open and
    hold this process's shutdown for uvicorn's whole graceful timeout (5 s),
    and with it the catalog lock a warm candidate is waiting for.
    """
    from zerg.services.runtime_admission import runtime_admission

    runtime = runtime_admission()
    if getattr(runtime, "state", None) not in {"draining", "drained"}:
        return False
    lifecycle = runtime.host_lifecycle()
    body = f"event: host_lifecycle\ndata: {json.dumps(lifecycle, separators=(',', ':'))}\n\n".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
    return True


class RuntimeWriteAdmissionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and _wants_event_stream(scope) and await _end_stream_with_lifecycle(scope, receive, send):
            return
        if scope["type"] in {"http", "websocket"} and not path.startswith(_INTERNAL_CONTROL_PREFIXES):
            if not await _await_catalog_handoff():
                content = _restarting_payload(path)
                if scope["type"] == "websocket":
                    await _deny_websocket(scope, send, content)
                    return
                body_complete = await _discard_body(receive)
                headers = {"Retry-After": "2"}
                if not body_complete:
                    headers["Connection"] = "close"
                await JSONResponse(status_code=503, content=content, headers=headers)(scope, receive, send)
                return
        if scope["type"] == "websocket" and path.rstrip("/") in _RUNTIME_WEBSOCKET_PATHS:
            from zerg.services.runtime_admission import runtime_admission

            runtime = runtime_admission()
            admitted, details = await runtime.try_admit(path=path)
            if not admitted:
                await _deny_websocket(scope, send, details)
                return

            released = False
            browser_socket = path.rstrip("/") == "/api/ws"

            async def release() -> None:
                nonlocal released
                if not released:
                    released = True
                    await runtime.release()

            if browser_socket:
                scope.setdefault("state", {})["runtime_admission_release"] = release

            async def send_and_release(message: Message) -> None:
                if message["type"] == "websocket.accept" and not browser_socket:
                    await release()
                await send(message)

            try:
                await self.app(scope, receive, send_and_release)
            finally:
                await release()
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") not in _MUTATING_METHODS or path.startswith(_INTERNAL_CONTROL_PREFIXES):
            await self.app(scope, receive, send)
            return

        from zerg.services.runtime_admission import runtime_admission

        runtime = runtime_admission()
        admitted, details = await runtime.try_admit(path=path)
        if not admitted:
            body_complete = await _discard_body(receive)
            content = {key: value for key, value in details.items() if key not in {"retry_after_seconds"}}
            headers = {"Retry-After": "2"}
            if not body_complete:
                # Do not reuse a connection whose rejected upload is incomplete.
                headers["Connection"] = "close"
            await JSONResponse(
                status_code=503,
                content=content,
                headers=headers,
            )(scope, receive, send)
            return

        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await runtime.release()

        async def send_and_release(message: Message) -> None:
            if message["type"] == "http.response.start":
                await release()
            await send(message)

        try:
            await self.app(scope, receive, send_and_release)
        finally:
            await release()
