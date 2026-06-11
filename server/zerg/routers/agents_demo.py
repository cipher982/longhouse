"""Agents API — demo seed and test cleanup endpoints."""

import logging
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.agents import AgentsStore
from zerg.services.demo_seed import delete_demo_sessions
from zerg.services.demo_seed import seed_missing_demo_sessions
from zerg.services.session_views import CleanupRequest
from zerg.services.session_views import CleanupResponse
from zerg.services.session_views import DemoSeedResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/demo", response_model=DemoSeedResponse)
async def seed_demo_sessions(
    replace: bool = Query(False, description="Delete existing demo sessions before seeding fresh demo data"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> DemoSeedResponse:
    """Seed missing demo sessions for the timeline (idempotent top-up)."""
    deleted_count = 0
    if replace:
        _settings = get_settings()
        if not _settings.auth_disabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Demo replace only available in dev mode (AUTH_DISABLED=1)",
            )
        deleted_count = delete_demo_sessions(db)

    seeded_count, failed_count = seed_missing_demo_sessions(db, now=datetime.now(timezone.utc))
    if failed_count:
        logger.warning(
            "Demo seed completed with %d failures and %d created sessions",
            failed_count,
            seeded_count,
        )
    return DemoSeedResponse(
        seeded=seeded_count > 0,
        sessions_created=seeded_count,
        sessions_failed=failed_count,
        sessions_deleted=deleted_count,
    )


@router.delete("/demo", response_model=DemoSeedResponse)
async def reset_demo_sessions(
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> DemoSeedResponse:
    """Delete all demo-seeded sessions (provider_session_id LIKE 'demo-%')."""
    _settings = get_settings()
    if not _settings.auth_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo reset only available in dev mode (AUTH_DISABLED=1)",
        )

    deleted = delete_demo_sessions(db)
    return DemoSeedResponse(
        seeded=False,
        sessions_created=deleted,
        sessions_deleted=deleted,
    )


@router.delete("/test-cleanup", response_model=CleanupResponse)
async def cleanup_test_sessions(
    body: CleanupRequest,
    db: Session = Depends(get_db),
) -> CleanupResponse:
    """Delete test sessions by project pattern (dev-only)."""
    _settings = get_settings()
    if not _settings.auth_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Test cleanup only available in dev mode (AUTH_DISABLED=1)",
        )

    store = AgentsStore(db)
    deleted = store.delete_sessions_by_project_patterns(body.project_patterns)

    return CleanupResponse(deleted=deleted)
