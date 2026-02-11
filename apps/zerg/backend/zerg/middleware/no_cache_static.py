"""Middleware that adds Cache-Control: no-store to static frontend assets.

Starlette's StaticFiles mount doesn't support custom response headers, so
this middleware intercepts responses for /assets and /frontend-static paths
and injects no-store to prevent browsers from caching stale JS/CSS bundles.
"""

from __future__ import annotations

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

_NO_CACHE_PREFIXES = ("/assets/", "/frontend-static/", "/static/")


class NoCacheStaticMiddleware:
    """Inject Cache-Control: no-store on static file responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if not any(path.startswith(p) for p in _NO_CACHE_PREFIXES):
            await self.app(scope, receive, send)
            return

        async def send_with_no_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Remove any existing Cache-Control header
                headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_no_cache)
