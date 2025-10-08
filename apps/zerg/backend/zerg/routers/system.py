"""System configuration & feature-flag endpoints (public).

Provides a *single* unauthenticated JSON endpoint so that the WASM frontend
can discover runtime flags (currently only `auth_disabled`) without the
developer having to keep environment variables in sync between the Python
and Rust build steps.
"""

from typing import Any
from typing import Dict

from fastapi import APIRouter
from fastapi import status
from sqlalchemy import text

from zerg.config import get_settings
from zerg.database import get_session_factory

try:  # optional – ws manager may not be present in minimal builds
    from zerg.websocket.manager import topic_manager  # type: ignore
except Exception:  # pragma: no cover – keep health resilient if import fails
    topic_manager = None  # type: ignore

router = APIRouter(prefix="/system", tags=["system"])

_settings = get_settings()


@router.get("/info", status_code=status.HTTP_200_OK)
def system_info() -> Dict[str, Any]:
    """Return non-sensitive runtime switches used by the SPA at startup."""

    return {
        "auth_disabled": _settings.auth_disabled,
        "google_client_id": _settings.google_client_id,
        # Surface public URL so frontend can compute callback routes when needed
        "app_public_url": _settings.app_public_url,
    }


@router.get("/health", status_code=status.HTTP_200_OK)
def health() -> Dict[str, Any]:
    """Lightweight readiness probe used by E2E tests.

    Returns JSON with overall status and basic subsystem stats. Keeps work
    minimal to avoid impacting test performance.
    """
    db_ok = True
    try:
        session_factory = get_session_factory()
        with session_factory() as s:  # type: ignore[arg-type]
            s.execute(text("SELECT 1"))
    except Exception:  # pragma: no cover – defensive: surface as unhealthy but do not raise
        db_ok = False

    ws_stats: Dict[str, Any] = {
        "available": topic_manager is not None,
    }
    if topic_manager is not None:
        try:
            ws_stats.update(
                active_connections=len(topic_manager.active_connections),
                topics=len(topic_manager.topic_subscriptions),
            )
        except Exception:  # pragma: no cover – never fail health due to stats
            pass

    return {
        "status": "ok" if db_ok else "degraded",
        "db": {"status": "ok" if db_ok else "error"},
        "ws": ws_stats,
    }
