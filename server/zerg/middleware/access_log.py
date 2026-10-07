"""Filtered request access log — the read-path forensic trail.

Uvicorn's own access log stays off because presence/heartbeat polls bury
everything useful. This records one line per real request so an incident can
answer who read what, from where, and when. Request bodies, headers, cookies,
query strings, and transcript content are never logged.

The same finished-request seam also feeds services/funnel_facts.py, which is off
unless a consenting tester's control plane turned it on; it keeps counts and
dates only and logs nothing.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send
from zerg.services.funnel_facts import observe_request as observe_funnel_request

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
    """Who the request resolved to, or an honest admission that we don't know.

    Auth dependencies stamp the request state during handling, so this is only
    meaningful once the handler has run. ``unattributed`` means no identity was
    recorded — a public route, a rejected request, or a handler that never
    stamps one — and deliberately does not read as the fact "an anonymous
    caller did this".
    """
    state = scope.get("state") or {}
    return str(state.get("principal") or state.get("agents_rate_key") or "unattributed")


def log_ws_principal(scope: Scope, principal: str) -> None:
    """Record a WebSocket principal that only became known after ``accept()``.

    For every socket that authenticates from the handshake -- a query token, a
    cookie, a header -- the endpoint stamps ``scope["state"]["principal"]``
    before accepting and the accept-time line carries it. ``/api/runners/ws``
    cannot: its secret arrives in the ``hello`` frame, so at accept the server
    genuinely does not know who is calling and the honest label is
    ``unattributed``. This writes the second, attributed line once it does,
    tied to the same path and client IP.

    Use it only where authentication is in-band. Anywhere the principal exists
    before accept, stamp the state instead: one line beats two.
    """
    logger.info(
        "WS %s authenticated",
        str(scope.get("path", "-")),
        extra={
            "principal": principal,
            "client_ip": _client_ip(scope),
            "tag": "access",
        },
    )


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
            await self._websocket(scope, receive, send, path)
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
            observe_funnel_request(scope, status_code)
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

    async def _websocket(self, scope: Scope, receive: Receive, send: Send, path: str) -> None:
        """Log one line per connection, once the handshake resolves.

        Not at connect: the endpoint authenticates after the scope reaches it,
        so a line written before then is always unattributed — worthless for
        exactly the streams (live transcripts) worth auditing. Not at close
        either: a stream can stay open for hours and the record is no use while
        it is still being read.

        Accept/reject is the first moment the principal exists for the two
        sockets that authenticate from the handshake itself: ``/api/ws``
        (Authorization header or session cookie) and ``/api/agents/control/ws``
        (device token header). Both resolve their caller before accepting and
        stamp ``scope["state"]["principal"]``, so the accept-time line carries it.

        ``/api/runners/ws`` is the exception: its secret arrives in the ``hello``
        frame, after accept, so its accept-time line is honestly
        ``unattributed`` and it emits a second attributed line through
        ``log_ws_principal`` once the frame authenticates.
        """
        logged = False

        def emit(outcome: str, code: object = "-") -> None:
            nonlocal logged
            if logged:
                return
            logged = True
            logger.info(
                "WS %s %s",
                path,
                outcome,
                extra={
                    "principal": _principal(scope),
                    "client_ip": _client_ip(scope),
                    # Close code for a rejected handshake, HTTP status for a
                    # denial, "-" otherwise.
                    "code": code,
                    "tag": "access",
                },
            )

        async def send_with_outcome(message: Message) -> None:
            if message["type"] == "websocket.accept":
                emit("accepted")
            elif message["type"] == "websocket.close":
                # A close *before* accept is a rejected handshake; a close after
                # one was already logged at accept and this is a no-op.
                emit("rejected", message.get("code", 1000))
            elif message["type"] == "websocket.http.response.start":
                # ASGI denial-response extension: the upgrade is answered with
                # an HTTP status (unroutable path, dependency failure) rather
                # than a close frame.
                emit("denied", message.get("status", "-"))
            await send(message)

        try:
            await self.app(scope, receive, send_with_outcome)
        finally:
            # The app returned without ever accepting or closing (an unhandled
            # error, or a disconnect during the handshake). Still record it.
            emit("unresolved")
