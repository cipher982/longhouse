"""System configuration & feature-flag endpoints (public).

Provides unauthenticated JSON endpoints so the frontend can discover runtime
flags without keeping environment variables in sync between build steps.
"""

from typing import Any
from typing import Dict

from fastapi import APIRouter
from fastapi import status
from sqlalchemy import text

from zerg.config import get_settings
from zerg.database import get_session_factory

router = APIRouter(prefix="/system", tags=["system"])

_settings = get_settings()


@router.get("/info", status_code=status.HTTP_200_OK)
def system_info() -> Dict[str, Any]:
    """Return non-sensitive runtime switches used by the SPA at startup."""

    return {
        "app_mode": _settings.app_mode.value,
        "auth_disabled": _settings.auth_disabled,
        "google_client_id": _settings.google_client_id,
        # Surface public URL so frontend can compute callback routes when needed
        "app_public_url": _settings.app_public_url,
        "public_site_url": _settings.public_site_url,
        "public_api_url": _settings.public_api_url,
        "demo_mode": _settings.demo_mode,
    }


@router.get("/capabilities", status_code=status.HTTP_200_OK)
def system_capabilities() -> Dict[str, Any]:
    """Return system capability flags for graceful degradation.

    Used by frontend to determine which features are available based on
    configured API keys and services.
    """
    llm_available = _settings.llm_available

    # Also check if any user has configured LLM provider keys via connectors
    if not llm_available:
        from zerg.connectors.registry import ConnectorType
        from zerg.models.models import AccountConnectorCredential

        session_factory = get_session_factory()
        with session_factory() as db:
            llm_types = [ConnectorType.OPENAI.value, ConnectorType.ANTHROPIC.value]
            has_llm_connector = (
                db.query(AccountConnectorCredential).filter(AccountConnectorCredential.connector_type.in_(llm_types)).first()
            ) is not None
            llm_available = has_llm_connector

    return {
        "llm_available": llm_available,
        "auth_disabled": _settings.auth_disabled,
    }


@router.post("/reset-sessions")
async def reset_sessions() -> Dict[str, Any]:
    """Clear all agent sessions (dev only).

    Used by ui-capture for deterministic empty state testing.
    Disabled in production.
    """
    if _settings.environment and _settings.environment.lower() == "production":
        return {"error": "Reset disabled in production"}

    session_factory = get_session_factory()
    with session_factory() as db:  # type: ignore[arg-type]
        # Delete events first (FK constraint), then sessions
        db.execute(text("DELETE FROM events"))
        db.execute(text("DELETE FROM sessions"))
        db.commit()

    return {"status": "ok", "message": "All sessions cleared"}


@router.post("/seed-demo-sessions")
async def seed_demo_sessions() -> Dict[str, Any]:
    """Seed demo agent sessions for marketing/onboarding.

    Public endpoint (no auth) for dev/demo purposes.
    Disabled in production unless demo_mode is enabled.
    """
    if _settings.environment and _settings.environment.lower() == "production" and not _settings.demo_mode:
        return {"error": "Demo seeding disabled in production"}

    from zerg.services.agents_store import AgentsStore
    from zerg.services.demo_sessions import build_demo_agent_sessions

    session_factory = get_session_factory()

    # Build demo sessions
    demo_sessions = build_demo_agent_sessions()

    # Ingest each session
    with session_factory() as db:  # type: ignore[arg-type]
        store = AgentsStore(db)
        for session in demo_sessions:
            store.ingest_session(session)
        db.commit()

    return {
        "status": "ok",
        "sessions_seeded": len(demo_sessions),
        "message": "Demo sessions seeded successfully",
    }
