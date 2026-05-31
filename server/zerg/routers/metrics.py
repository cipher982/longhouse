"""/metrics endpoint that exposes Prometheus counters in text-format."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status

from zerg.config import get_settings

# The import might fail in extremely minimal test environments where the
# optional dependency was skipped.  In that case we still register the route
# but return *501 Not Implemented* so monitoring can detect the misconfig.


router = APIRouter(tags=["metrics"], include_in_schema=False)
logger = logging.getLogger(__name__)


def _metrics_access_allowed(request: Request) -> bool:
    """Gate /metrics so it is not a public scrape/info-leak + DB-load surface.

    Allowed for: loopback callers (only when no public origin is configured — a
    reverse proxy makes public traffic look loopback), a caller presenting
    LONGHOUSE_METRICS_TOKEN (Authorization: Bearer / X-Metrics-Token), or the
    internal API secret. Otherwise denied. auth_disabled is NOT a remote bypass.
    """
    settings = get_settings()

    client_host = request.client.host if request.client else None
    public_origin_configured = bool(settings.public_site_url or settings.app_public_url or settings.public_api_url)
    if client_host == "testclient":
        return True
    if not public_origin_configured and client_host in ("127.0.0.1", "::1", "localhost"):
        return True

    metrics_token = os.environ.get("LONGHOUSE_METRICS_TOKEN", "").strip()
    if metrics_token:
        presented = request.headers.get("X-Metrics-Token") or ""
        auth_header = request.headers.get("Authorization") or ""
        if auth_header.lower().startswith("bearer "):
            presented = presented or auth_header[7:].strip()
        if presented and presented == metrics_token:
            return True

    internal = request.headers.get("X-Internal-Token")
    if internal and settings.internal_api_secret and internal == settings.internal_api_secret:
        return True

    return False


def _refresh_dynamic_gauges() -> None:
    try:
        from zerg.database import get_session_factory
        from zerg.services.session_runtime import refresh_managed_codex_liveness_metrics

        session_factory = get_session_factory()
        with session_factory() as db:
            refresh_managed_codex_liveness_metrics(db)
    except Exception:
        logger.exception("Failed to refresh dynamic metrics gauges")


try:
    from prometheus_client import CONTENT_TYPE_LATEST  # type: ignore
    from prometheus_client import generate_latest  # type: ignore

    @router.get("/metrics")
    def metrics(request: Request) -> Response:  # noqa: D401 – external signature
        # Deny (404, hide existence) before doing any DB work on the refresh.
        if not _metrics_access_allowed(request):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        _refresh_dynamic_gauges()
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

except ModuleNotFoundError:  # pragma: no cover – metrics disabled

    @router.get("/metrics")
    def metrics_na(request: Request) -> Response:  # noqa: D401 – external signature
        """Return 501 if prometheus_client is missing — still access-gated."""
        if not _metrics_access_allowed(request):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return Response(
            content='{"error": "prometheus_client not installed"}',
            media_type="application/json",
            status_code=501,
        )
