"""Filtered request access log — the read-path forensic trail.

Uvicorn's own access log stays off because presence/heartbeat polls bury
everything useful. This records one line per real request so an incident can
answer who read what, from where, and when. Request bodies, tokens, cookies,
and transcript content are never logged.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

logger = logging.getLogger(__name__)

# High-frequency polls and static assets: pure noise, and none of them read
# user data.
_SKIP_PREFIXES = (
    "/api/agents/heartbeat",
    "/api/agents/presence",
    "/api/agents/machine-presence",
    "/api/agents/machines/health",
    "/api/agents/session-state/health",
    "/api/users/me/client-presence",
    "/api/health",
    "/metrics",
    "/assets/",
    "/frontend-static/",
    "/static/",
)


def _client_ip(scope: Scope) -> str:
    # Behind a reverse proxy the peer is the proxy; the last X-Forwarded-For
    # entry is the one our own proxy appended, so earlier spoofed entries
    # can't shadow the real client.
    for key, value in scope.get("headers", ()):
        if key == b"x-forwarded-for" and value.strip():
            return value.decode("latin-1").split(",")[-1].strip()
    client = scope.get("client")
    return client[0] if client else "-"


def _principal(scope: Scope) -> str:
    # Auth dependencies stamp the request state during handling; read it after
    # the response so the identity is resolved.
    state = scope.get("state") or {}
    return str(state.get("principal") or state.get("agents_rate_key") or "anonymous")


class AccessLogMiddleware:
    """Log one line per non-noise request. Pure ASGI — never touches the body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if scope["type"] not in ("http", "websocket") or path.startswith(_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Log at connect: a stream can stay open for hours, and a record
            # written at close is no use while it's still being read.
            logger.info("WS %s", path, extra={"principal": _principal(scope), "client_ip": _client_ip(scope), "tag": "access"})
            await self.app(scope, receive, send)
            return

        status_code = 0
        started = time.monotonic()

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            logger.info(
                "%s %s %s",
                scope.get("method", "-"),
                path,
                status_code,
                extra={
                    "principal": _principal(scope),
                    "client_ip": _client_ip(scope),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "tag": "access",
                },
            )
