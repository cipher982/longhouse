"""FastAPI / Starlette middleware that routes requests coming from different
*Playwright* workers to *isolated* Postgres schemas (E2E test DB isolation).

The Playwright side injects the current worker index via the HTTP header
``X-Test-Worker`` **and** appends the query parameter ``?worker=<id>`` to every
WebSocket URL.  The middleware extracts that identifier and stores it inside a
``contextvars.ContextVar`` so that :pymod:`zerg.database` can select the
appropriate SQLAlchemy *Session* factory.

When the header / query parameter is **missing** the application falls back to
the *default* (shared) engine so normal development usage and unit-tests stay
unchanged.
"""

from __future__ import annotations

import contextvars

from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

# ---------------------------------------------------------------------------
# Public accessor so zerg.database can import the variable without a dependency
# cycle (middleware <-> database).
# ---------------------------------------------------------------------------

current_worker_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_worker_id", default=None)


class WorkerDBMiddleware:  # noqa: D401 – ASGI middleware
    """Attach *Playwright worker id* to the request context (HTTP & WS)."""

    def __init__(self, app: ASGIApp) -> None:  # noqa: D401 – ASGI signature
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: D401 – ASGI
        scope_type = scope.get("type")
        worker_id: str | None = None

        # ------------------------------------------------------------------
        # 1. Grab header `X-Test-Worker: <id>` for *HTTP* requests (REST and
        #    WebSocket *handshake*).
        # ------------------------------------------------------------------
        if scope_type in {"http", "websocket"}:
            for raw_name, raw_value in scope.get("headers", []):
                if raw_name.lower() == b"x-test-worker":
                    worker_id = raw_value.decode()
                    break

            # ----------------------------------------------------------------
            # 2. Fallback: query parameter `?worker=<id>` – used for WebSocket
            #    upgrades because browsers do not expose custom headers when
            #    the JS `WebSocket` constructor is called.
            # ----------------------------------------------------------------
            if worker_id is None and (qs := scope.get("query_string")):
                try:
                    query_str = qs.decode()
                except Exception:  # pragma: no cover – extremely unlikely
                    query_str = ""

                for param in query_str.split("&"):
                    if param.startswith("worker="):
                        worker_id = param.split("=", 1)[1]
                        break

        # ------------------------------------------------------------------
        # If we *did* detect a worker id push it into the context var for the
        # duration of the request / websocket lifespan.
        # ------------------------------------------------------------------
        token = None
        if worker_id is not None:
            token = current_worker_id.set(worker_id)

        try:
            await self.app(scope, receive, send)
        finally:
            # Reset so subsequent requests on the *same* event-loop worker do
            # not inherit the previous id.
            if token is not None:
                current_worker_id.reset(token)
