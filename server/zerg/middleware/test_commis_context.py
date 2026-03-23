"""Middleware for setting test commis context during E2E runs.

Routes DB sessions to commis-specific SQLite files based on the X-Test-Commis
header (or commis query param for non-header clients).
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from zerg.config import get_settings
from zerg.database import reset_test_commis_id
from zerg.database import set_test_commis_id


class TestCommisContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.testing:
            return await call_next(request)

        commis_id = request.headers.get("X-Test-Commis") or request.query_params.get("commis")
        if not commis_id:
            return await call_next(request)

        token = set_test_commis_id(commis_id)
        try:
            return await call_next(request)
        finally:
            reset_test_commis_id(token)
