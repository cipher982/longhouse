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

from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_INTERNAL_CONTROL_PREFIXES = ("/internal/deployments/", "/api/internal/deployments/")


class RuntimeWriteAdmissionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if scope.get("method") not in _MUTATING_METHODS or path.startswith(_INTERNAL_CONTROL_PREFIXES):
            await self.app(scope, receive, send)
            return

        from zerg.services.runtime_admission import runtime_admission

        admitted, details = await runtime_admission().try_admit(path=path)
        if not admitted:
            await JSONResponse(status_code=503, content=details)(scope, receive, send)
            return

        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await runtime_admission().release()

        async def send_and_release(message: Message) -> None:
            if message["type"] == "http.response.start":
                await release()
            await send(message)

        try:
            await self.app(scope, receive, send_and_release)
        finally:
            await release()
