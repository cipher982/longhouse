"""ASGI middleware that blocks write operations in demo mode.

When DEMO_MODE is enabled, all non-safe HTTP methods (POST, PUT, PATCH, DELETE)
on /api/* paths are rejected with 403, except for an explicit allowlist of
read-like POST endpoints that must remain functional.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from typing import Callable

logger = logging.getLogger(__name__)

# POST endpoints that are read-like or required for demo functionality.
# Paths are matched as prefixes (startswith).
_ALLOWED_POST_PREFIXES: tuple[str, ...] = (
    "/api/agents/demo",
    "/api/system/seed-demo-sessions",
    "/api/auth/",
    "/api/waitlist",
)

_SAFE_METHODS: frozenset[str] = frozenset({b"GET", b"HEAD", b"OPTIONS"})

_BLOCKED_RESPONSE_BODY = json.dumps({"error": "Demo mode \u2014 read-only", "demo": True}).encode("utf-8")

_BLOCKED_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_BLOCKED_RESPONSE_BODY)).encode()),
]


class DemoGuardMiddleware:
    """Pure ASGI middleware — no BaseHTTPMiddleware body-consumption issues."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable,
        send: Callable,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: bytes = scope.get("method", "GET").encode() if isinstance(scope.get("method"), str) else scope.get("method", b"GET")
        path: str = scope.get("path", "")

        # Only guard /api/* paths
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # Safe methods always pass
        if method in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        # Check allowlist for non-safe methods
        for prefix in _ALLOWED_POST_PREFIXES:
            if path.startswith(prefix):
                await self.app(scope, receive, send)
                return

        # Block the request
        logger.info("Demo guard blocked %s %s", method, path)
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": _BLOCKED_HEADERS,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": _BLOCKED_RESPONSE_BODY,
            }
        )
