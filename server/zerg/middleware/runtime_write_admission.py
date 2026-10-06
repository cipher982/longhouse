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

import json

from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_INTERNAL_CONTROL_PREFIXES = ("/internal/deployments/", "/api/internal/deployments/")
_RUNTIME_WEBSOCKET_PATHS = frozenset({"/api/agents/control/ws", "/api/runners/ws"})


async def _discard_body(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        if message["type"] == "http.request" and not message.get("more_body", False):
            return


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


class RuntimeWriteAdmissionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "websocket" and path.rstrip("/") in _RUNTIME_WEBSOCKET_PATHS:
            from zerg.services.runtime_admission import runtime_admission

            runtime = runtime_admission()
            admitted, details = await runtime.try_admit(path=path)
            if not admitted:
                await _deny_websocket(scope, send, details)
                return

            released = False

            async def release() -> None:
                nonlocal released
                if not released:
                    released = True
                    await runtime.release()

            async def send_and_release(message: Message) -> None:
                if message["type"] == "websocket.accept":
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
            await _discard_body(receive)
            content = {key: value for key, value in details.items() if key not in {"retry_after_seconds"}}
            await JSONResponse(
                status_code=503,
                content=content,
                headers={"Retry-After": "2"},
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
