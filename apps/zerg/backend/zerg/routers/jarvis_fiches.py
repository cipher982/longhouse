"""Jarvis fiche listing endpoints."""

import logging
from datetime import datetime
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.routers.jarvis_auth import get_current_jarvis_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


class JarvisFicheSummary(BaseModel):
    """Minimal fiche summary for Jarvis UI."""

    id: int
    name: str
    status: str
    schedule: Optional[str] = None
    next_course_at: Optional[datetime] = None
    description: Optional[str] = None


@router.get("/fiches", response_model=List[JarvisFicheSummary])
def list_jarvis_fiches(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> List[JarvisFicheSummary]:
    """List available fiches for Jarvis UI.

    Returns a minimal summary of all active fiches including their schedules
    and next course times. This powers the fiche selection UI in Jarvis.

    Args:
        db: Database session
        current_user: Authenticated user (Jarvis service account)

    Returns:
        List of fiche summaries
    """
    # Multi-tenant SaaS: Jarvis shows only the logged-in user's fiches.
    fiches = crud.get_fiches(db, owner_id=current_user.id)

    summaries = []
    for fiche in fiches:
        # Calculate next_course_at from schedule if present
        next_course_at = None
        if fiche.schedule:
            # TODO: Parse cron schedule and calculate next course
            # For now, leave as None - implement in Phase 4
            pass

        summaries.append(
            JarvisFicheSummary(
                id=fiche.id,
                name=fiche.name,
                status=fiche.status.value if hasattr(fiche.status, "value") else str(fiche.status),
                schedule=fiche.schedule,
                next_course_at=next_course_at,
                description=fiche.system_instructions[:200] if fiche.system_instructions else None,
            )
        )

    return summaries
