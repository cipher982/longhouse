"""Request-level timeout middleware for API endpoints.

If a SQLite query blocks for the full busy_timeout (30s), the request hangs
with no response and eventually all uvicorn connections get consumed.  This
middleware enforces a maximum response time on /api/ routes and returns a
503 Service Unavailable when the deadline is exceeded.

Implemented as a raw ASGI middleware (not Starlette BaseHTTPMiddleware) to
avoid the known issues with BaseHTTPMiddleware and streaming responses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15
RECALL_TIMEOUT_SECONDS = 5
MANAGED_LOCAL_LAUNCH_TIMEOUT_SECONDS = 45
INTERACTIVE_AUTH_TIMEOUT_SECONDS = 30
INGEST_TIMEOUT_SECONDS = 30
ARCHIVE_BUNDLE_TIMEOUT_SECONDS = 120
ARCHIVE_READ_TIMEOUT_SECONDS = 60

# Paths that are excluded from the timeout enforcement.
_SKIP_PATHS = ("/readyz", "/health")

# Path fragments that indicate a streaming / long-lived connection.
_STREAMING_FRAGMENTS = ("/stream", "/chat", "/branch", "/ws")

# Route-specific timeout overrides for legitimate long-running requests.
_TIMEOUT_OVERRIDES = {
    "/agents/recall": RECALL_TIMEOUT_SECONDS,
    "/devices/tokens": INTERACTIVE_AUTH_TIMEOUT_SECONDS,
    "/agents/ingest": INGEST_TIMEOUT_SECONDS,
    "/sessions/managed-local/this-device": MANAGED_LOCAL_LAUNCH_TIMEOUT_SECONDS,
    "/agents/sessions/": ARCHIVE_BUNDLE_TIMEOUT_SECONDS,
}

_ARCHIVE_READ_EXACT_PATHS = {
    "/agents/ingest-health",
    "/agents/machines/health",
    "/agents/usage-stats",
    "/timeline/filters",
    "/timeline/recall",
    "/timeline/sessions/semantic",
    "/timeline/sessions/summary",
}


def _uses_archive_read_timeout(api_path: str, method: str) -> bool:
    if method not in {"GET", "HEAD"}:
        return False
    return (
        api_path in _ARCHIVE_READ_EXACT_PATHS
        or api_path.startswith("/agents/worklog/")
        or api_path.startswith("/agents/sessions/")
        or api_path.startswith("/timeline/sessions/")
        or api_path.startswith("/timeline/workflows/")
        or api_path.startswith("/timeline/session-shares/")
    )


class RequestTimeoutMiddleware:
    """ASGI middleware that enforces a max response time on /api/ requests.

    Routes that are inherently long-lived (WebSocket upgrades, SSE streams,
    health probes) are excluded.  Everything else under /api/ gets a hard
    deadline; on expiry the client receives ``503 {"detail": "Request timed out"}``.
    """

    def __init__(self, app: ASGIApp, timeout: float | None = None) -> None:
        self.app = app
        self.timeout = timeout if timeout is not None else float(os.environ.get("REQUEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only apply to HTTP requests (skip WebSocket entirely).
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Only enforce on /api/ routes.
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # The api sub-app is mounted at /api, so inner paths have the prefix
        # stripped.  But on the *parent* app where this middleware lives, we
        # still see the full path.  Normalise for the skip checks by looking
        # at the portion after /api.
        api_path = path[4:]  # "/api/health" -> "/health"
        started_at = time.perf_counter()
        if api_path == "/agents/recall":
            scope.setdefault("state", {})["request_timeout_started_at"] = started_at

        # Skip health/readiness probes (they have their own timeout handling).
        if api_path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        # Skip SSE / streaming / WebSocket-upgrade endpoints.
        if any(frag in api_path for frag in _STREAMING_FRAGMENTS):
            await self.app(scope, receive, send)
            return

        timeout = self.timeout
        method = str(scope.get("method") or "GET").upper()
        if _uses_archive_read_timeout(api_path, method):
            timeout = ARCHIVE_READ_TIMEOUT_SECONDS
        for prefix, override in _TIMEOUT_OVERRIDES.items():
            if prefix == "/agents/sessions/":
                if api_path.startswith(prefix) and api_path.endswith("/archive-bundle"):
                    timeout = override
                    break
                continue
            if api_path.startswith(prefix):
                timeout = override
                break

        # Enforce timeout.
        response_started = False

        async def send_wrapper(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await asyncio.wait_for(
                self.app(scope, receive, send_wrapper),
                timeout=timeout,
            )
            elapsed = time.perf_counter() - started_at
            if api_path == "/agents/recall" and elapsed > 1.0:
                logger.warning(
                    "Slow recall HTTP request completed elapsed_ms=%.1f path=%s",
                    elapsed * 1000,
                    path,
                )
        except asyncio.TimeoutError:
            method = scope.get("method", "?")
            logger.warning(
                "Request timed out after %.1fs: %s %s",
                timeout,
                method,
                path,
            )

            if response_started:
                # Headers already sent — we cannot emit a new response.
                # The connection will be closed by the server.
                return

            body = b'{"detail":"Request timed out"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("latin-1")),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                }
            )
