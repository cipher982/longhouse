"""Middleware that sets cache headers for static frontend assets.

Starlette's StaticFiles mount doesn't support custom response headers, so
this middleware intercepts responses for static frontend paths and injects
cache headers that match asset type.
"""

from __future__ import annotations

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

_STATIC_CACHE_PREFIXES = ("/assets/", "/frontend-static/", "/static/")
_IMMUTABLE_CACHE_PREFIXES = ("/assets/",)


def _cache_control_for_path(path: str) -> bytes | None:
    if path.startswith(_IMMUTABLE_CACHE_PREFIXES):
        return b"public, max-age=31536000, immutable"
    if path.startswith(_STATIC_CACHE_PREFIXES):
        return b"public, max-age=86400, stale-while-revalidate=604800"
    return None


class NoCacheStaticMiddleware:
    """Inject static asset cache headers on static file responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        cache_control = _cache_control_for_path(path)
        if cache_control is None:
            await self.app(scope, receive, send)
            return

        async def send_with_static_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                headers.append((b"cache-control", cache_control))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_static_cache)
