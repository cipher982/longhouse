"""Oikos task listing endpoints."""

import logging
from datetime import datetime
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from zerg.crud import get_fiches
from zerg.database import get_db
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["oikos"])


class OikosTaskSummary(UTCBaseModel):
    """Minimal task summary for Oikos UI."""

    id: int
    name: str
    status: str
    schedule: Optional[str] = None
    next_run_at: Optional[datetime] = None
    description: Optional[str] = None


@router.get("/tasks", response_model=List[OikosTaskSummary])
def list_oikos_tasks(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[OikosTaskSummary]:
    """List the current user's tasks with schedule and status summaries."""
    tasks = get_fiches(db, owner_id=current_user.id)

    summaries = []
    for task in tasks:
        summaries.append(
            OikosTaskSummary(
                id=task.id,
                name=task.name,
                status=task.status.value if hasattr(task.status, "value") else str(task.status),
                schedule=task.schedule,
                next_run_at=task.next_run_at,
                description=task.system_instructions[:200] if task.system_instructions else None,
            )
        )

    return summaries
