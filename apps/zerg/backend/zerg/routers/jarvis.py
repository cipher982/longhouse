"""Jarvis Integration API Router.

Provides endpoints for Jarvis (voice/text UI) to interact with Zerg backend:
- Authentication: Device secret → JWT token
- Agent listing: Get available agents with schedules
- Run history: Recent agent executions with summaries
- Dispatch: Trigger agent tasks from Jarvis
- Events: SSE stream for real-time updates
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.enums import AgentStatus, RunStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jarvis", tags=["jarvis"])


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class JarvisAuthRequest(BaseModel):
    """Jarvis authentication request with device secret."""

    device_secret: str = Field(..., description="Device secret for Jarvis authentication")


class JarvisAuthResponse(BaseModel):
    """Jarvis authentication response with JWT token."""

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiry in seconds")


class JarvisAgentSummary(BaseModel):
    """Minimal agent summary for Jarvis UI."""

    id: int
    name: str
    status: str
    schedule: Optional[str] = None
    next_run_at: Optional[datetime] = None
    description: Optional[str] = None


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


# ---------------------------------------------------------------------------
# Authentication Endpoint
# ---------------------------------------------------------------------------


@router.post("/auth", response_model=JarvisAuthResponse)
def jarvis_auth(
    request: JarvisAuthRequest,
    db: Session = Depends(get_db),
) -> JarvisAuthResponse:
    """Authenticate Jarvis device and return JWT token.

    Validates the device secret against environment configuration and issues
    a short-lived JWT token that Jarvis can use for subsequent API calls.

    Args:
        request: Contains device_secret for authentication
        db: Database session

    Returns:
        JarvisAuthResponse with JWT token

    Raises:
        401: Invalid device secret
    """
    settings = get_settings()

    # Validate device secret from environment
    expected_secret = getattr(settings, "jarvis_device_secret", None)
    if not expected_secret:
        logger.error("JARVIS_DEVICE_SECRET not configured in environment")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Jarvis authentication not configured",
        )

    if request.device_secret != expected_secret:
        logger.warning("Invalid Jarvis device secret attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device secret",
        )

    # Issue JWT token for Jarvis (longer expiry for device auth)
    # Import the token issuing function from auth router
    from zerg.routers.auth import _issue_access_token

    # Create token with jarvis scope - expires in 7 days for device auth
    token_expiry_seconds = 60 * 60 * 24 * 7  # 7 days
    token = _issue_access_token(
        user_id=0,  # Special ID for Jarvis service account
        email="jarvis@swarm.local",
        display_name="Jarvis",
        expires_delta=timedelta(seconds=token_expiry_seconds),
    )

    logger.info("Issued JWT token for Jarvis device")

    return JarvisAuthResponse(
        access_token=token,
        token_type="bearer",
        expires_in=token_expiry,
    )


# ---------------------------------------------------------------------------
# Agent Listing Endpoint
# ---------------------------------------------------------------------------


@router.get("/agents", response_model=List[JarvisAgentSummary])
def list_jarvis_agents(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> List[JarvisAgentSummary]:
    """List available agents for Jarvis UI.

    Returns a minimal summary of all active agents including their schedules
    and next run times. This powers the agent selection UI in Jarvis.

    Args:
        db: Database session
        current_user: Authenticated user (Jarvis service account)

    Returns:
        List of agent summaries
    """
    # Get all agents (Jarvis has admin access, sees all agents)
    agents = crud.get_agents(db)

    summaries = []
    for agent in agents:
        # Calculate next_run_at from schedule if present
        next_run_at = None
        if agent.schedule:
            # TODO: Parse cron schedule and calculate next run
            # For now, leave as None - implement in Phase 4
            pass

        summaries.append(
            JarvisAgentSummary(
                id=agent.id,
                name=agent.name,
                status=agent.status.value if hasattr(agent.status, "value") else str(agent.status),
                schedule=agent.schedule,
                next_run_at=next_run_at,
                description=agent.system_instructions[:200] if agent.system_instructions else None,
            )
        )

    return summaries


# ---------------------------------------------------------------------------
# Run History Endpoint
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=List[JarvisRunSummary])
def list_jarvis_runs(
    limit: int = 50,
    agent_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
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

    query = db.query(crud.models.AgentRun)

    if agent_id:
        query = query.filter(crud.models.AgentRun.agent_id == agent_id)

    runs = query.order_by(crud.models.AgentRun.created_at.desc()).limit(limit).all()

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
                completed_at=getattr(run, "completed_at", None),
            )
        )

    return summaries
