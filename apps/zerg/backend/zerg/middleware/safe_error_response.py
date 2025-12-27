"""Response-start-aware error handling middleware.

This middleware enforces HTTP protocol correctness for error responses:
- If an exception occurs BEFORE http.response.start: emit a proper JSON 500 with CORS headers
- If an exception occurs AFTER http.response.start: don't try to send anything new (protocol violation)

This helps avoid one common source of "Too much data for declared Content-Length" cascades:
when exception handlers attempt to emit a fresh JSON 500 after headers have already been sent.
Note that h11 Content-Length mismatches can also be triggered by other paths (middleware
ordering with compression, status codes without bodies, etc.) - this addresses the
exception-handler-after-start case specifically.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Transport-layer exceptions that are expected during client disconnects / proxy weirdness.
# These are NOT app bugs - they're just "connection went away mid-response".
# We suppress these at DEBUG level to avoid log spam during long dev sessions.
TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)

# Try to add h11's LocalProtocolError if available
try:
    from h11._util import LocalProtocolError  # type: ignore[import-untyped]

    TRANSPORT_EXCEPTIONS = (*TRANSPORT_EXCEPTIONS, LocalProtocolError)
except ImportError:
    pass

# Try to add anyio's EndOfStream if available
try:
    from anyio import EndOfStream

    TRANSPORT_EXCEPTIONS = (*TRANSPORT_EXCEPTIONS, EndOfStream)
except ImportError:
    pass


class SafeErrorResponseMiddleware:
    """Middleware that handles exceptions while respecting HTTP protocol constraints.

    This replaces:
    - ensure_cors_on_errors exception handler (tried to send JSON after response started)

    Key behavior:
    - Pre-response-start exceptions: send JSON 500 with CORS headers, log at ERROR
    - Post-response-start transport errors: log at DEBUG (expected churn)
    - Post-response-start app errors: log at ERROR but don't try to send (would violate HTTP)
    """

    def __init__(self, app: ASGIApp, cors_origins: Sequence[str] | None = None) -> None:
        self.app = app
        # Store cors_origins at init time. Tests can patch self._cors_origins directly.
        self._cors_origins: list[str] = list(cors_origins) if cors_origins else []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if response_started:
                # Response already started - we CANNOT send a new response.
                # Distinguish between transport churn (expected) and app bugs (unexpected).
                if isinstance(exc, TRANSPORT_EXCEPTIONS):
                    # Transport-layer: client disconnected, proxy weirdness, etc.
                    # Log at DEBUG to keep signal without overnight spam.
                    logger.debug(
                        "Transport exception after response started (expected): %s: %s",
                        type(exc).__name__,
                        str(exc)[:200],
                    )
                else:
                    # App bug during streaming - still can't send a new response,
                    # but this IS unexpected and should be visible.
                    logger.error(
                        "Exception after response started (cannot send error response): %s",
                        exc,
                        exc_info=True,
                    )
                # Don't re-raise - the connection is already being torn down
                return

            # Response NOT started - we can safely send a JSON error with CORS headers
            logger.error("Unhandled exception: %s", exc, exc_info=True)

            # Build CORS headers if origin matches
            origin = None
            for header_name, header_value in scope.get("headers", []):
                if header_name == b"origin":
                    origin = header_value.decode("latin-1")
                    break

            headers: list[tuple[bytes, bytes]] = [
                (b"content-type", b"application/json"),
                (b"vary", b"Origin"),
            ]

            if origin and self._origin_allowed(origin):
                # Note: When cors_origins contains "*", we still echo the specific origin
                # (not literal "*") because Access-Control-Allow-Credentials: true requires it.
                # Browsers reject `Access-Control-Allow-Origin: *` with credentials.
                headers.extend(
                    [
                        (b"access-control-allow-origin", origin.encode("latin-1")),
                        (b"access-control-allow-credentials", b"true"),
                        (b"access-control-allow-methods", b"*"),
                        (b"access-control-allow-headers", b"*"),
                    ]
                )

            body = b'{"detail":"Internal server error"}'
            headers.append((b"content-length", str(len(body)).encode("latin-1")))

            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": headers,
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                }
            )

    def _origin_allowed(self, origin: str) -> bool:
        """Check if origin is in the allowed list."""
        if "*" in self._cors_origins:
            return True
        return origin in self._cors_origins
