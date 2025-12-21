"""Jarvis run history endpoints."""

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
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.routers.jarvis_auth import get_current_jarvis_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


class JarvisRunSummary(BaseModel):
    """Minimal run summary for Jarvis Task Inbox."""

    id: int
    agent_id: int
    agent_name: str
    status: str
    summary: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


@router.get("/runs", response_model=List[JarvisRunSummary])
def list_jarvis_runs(
    limit: int = 50,
    agent_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> List[JarvisRunSummary]:
    """List recent agent runs for Jarvis Task Inbox.

    Returns recent run history with summaries, filtered by agent if specified.
    This powers the Task Inbox UI in Jarvis showing all automated activity.

    Args:
        limit: Maximum number of runs to return (default 50)
        agent_id: Optional filter by specific agent
        db: Database session
        current_user: Authenticated user (Jarvis service account)

    Returns:
        List of run summaries ordered by created_at descending
    """
    # Get recent runs
    # TODO: Add crud method for filtering by agent_id and ordering by created_at
    # For now, get all runs and filter/sort in memory

    # Multi-tenant SaaS: Jarvis shows only the logged-in user's runs.
    query = db.query(AgentRun).join(Agent, Agent.id == AgentRun.agent_id).filter(Agent.owner_id == current_user.id)

    if agent_id:
        query = query.filter(AgentRun.agent_id == agent_id)

    runs = query.order_by(AgentRun.created_at.desc()).limit(limit).all()

    summaries = []
    for run in runs:
        # Get agent name
        agent = crud.get_agent(db, run.agent_id)
        agent_name = agent.name if agent else f"Agent {run.agent_id}"

        # Extract summary from run (will be populated in Phase 2.3)
        summary = getattr(run, "summary", None)

        summaries.append(
            JarvisRunSummary(
                id=run.id,
                agent_id=run.agent_id,
                agent_name=agent_name,
                status=run.status.value if hasattr(run.status, "value") else str(run.status),
                summary=summary,
                created_at=run.created_at,
                updated_at=run.updated_at,
                completed_at=run.finished_at,
            )
        )

    return summaries
