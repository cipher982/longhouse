"""Test-only HTTP routing for per-worker SQLite databases.

E2E browser/API requests send ``X-Test-Worker`` so each Playwright worker can
use an isolated SQLite file. WebSocket routes already pass ``worker=...``
explicitly; this middleware brings ordinary HTTP requests onto the same
ContextVar-based routing path.
"""

from __future__ import annotations

from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send
from zerg.database import reset_test_worker_id
from zerg.database import set_test_worker_id


class E2EWorkerRoutingMiddleware:
    """Set the active test worker id for HTTP requests in E2E mode."""

    def __init__(self, app: ASGIApp, *, enabled: bool = True) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        worker_id = _extract_worker_id(scope)
        if not worker_id:
            await self.app(scope, receive, send)
            return

        token = set_test_worker_id(worker_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_test_worker_id(token)


def _extract_worker_id(scope: Scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() != b"x-test-worker":
            continue
        try:
            worker_id = value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        return worker_id or None
    return None
