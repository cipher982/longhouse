"""Oikos fiche listing endpoints."""

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
from zerg.routers.oikos_auth import get_current_oikos_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["oikos"])


class OikosFicheSummary(BaseModel):
    """Minimal fiche summary for Oikos UI."""

    id: int
    name: str
    status: str
    schedule: Optional[str] = None
    next_run_at: Optional[datetime] = None
    description: Optional[str] = None


@router.get("/fiches", response_model=List[OikosFicheSummary])
def list_oikos_fiches(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[OikosFicheSummary]:
    """List available fiches for Oikos UI.

    Returns a minimal summary of all active fiches including their schedules
    and next run times. This powers the fiche selection UI in Oikos.

    Args:
        db: Database session
        current_user: Authenticated user (Oikos service account)

    Returns:
        List of fiche summaries
    """
    # Multi-tenant SaaS: Oikos shows only the logged-in user's fiches.
    fiches = crud.get_fiches(db, owner_id=current_user.id)

    summaries = []
    for fiche in fiches:
        # Calculate next_run_at from schedule if present
        next_run_at = None
        if fiche.schedule:
            # TODO: Parse cron schedule and calculate next run
            # For now, leave as None - implement in Phase 4
            pass

        summaries.append(
            OikosFicheSummary(
                id=fiche.id,
                name=fiche.name,
                status=fiche.status.value if hasattr(fiche.status, "value") else str(fiche.status),
                schedule=fiche.schedule,
                next_run_at=next_run_at,
                description=fiche.system_instructions[:200] if fiche.system_instructions else None,
            )
        )

    return summaries
