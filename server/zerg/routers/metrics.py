"""/metrics endpoint that exposes Prometheus counters in text-format."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Response

# The import might fail in extremely minimal test environments where the
# optional dependency was skipped.  In that case we still register the route
# but return *501 Not Implemented* so monitoring can detect the misconfig.


router = APIRouter(tags=["metrics"], include_in_schema=False)


try:
    from prometheus_client import CONTENT_TYPE_LATEST  # type: ignore
    from prometheus_client import generate_latest  # type: ignore

    @router.get("/metrics")
    def metrics() -> Response:  # noqa: D401 – external signature
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

except ModuleNotFoundError:  # pragma: no cover – metrics disabled

    @router.get("/metrics", status_code=501)
    def metrics_na() -> dict[str, str]:  # noqa: D401 – external signature
        """Return 501 if prometheus_client is missing at runtime."""

        return {"error": "prometheus_client not installed"}
