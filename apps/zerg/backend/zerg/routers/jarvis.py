"""Jarvis Integration API Router.

Provides endpoints for Jarvis (voice/text UI) to interact with Zerg backend:
- Authentication: Device secret → JWT token
- Agent listing: Get available agents with schedules
- Run history: Recent agent executions with summaries
- Dispatch: Trigger agent tasks from Jarvis
- Events: SSE stream for real-time updates
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.enums import AgentStatus, RunStatus, RunTrigger
from zerg.services.task_runner import execute_agent_task

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


class JarvisDispatchRequest(BaseModel):
    """Jarvis dispatch request to trigger agent execution."""

    agent_id: int = Field(..., description="ID of agent to execute")
    task_override: Optional[str] = Field(None, description="Optional task instruction override")


class JarvisDispatchResponse(BaseModel):
    """Jarvis dispatch response with run/thread IDs."""

    run_id: int = Field(..., description="AgentRun ID for tracking execution")
    thread_id: int = Field(..., description="Thread ID containing conversation")
    status: str = Field(..., description="Initial run status")
    agent_name: str = Field(..., description="Name of agent being executed")


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

# ---------------------------------------------------------------------------
# Dispatch Endpoint
# ---------------------------------------------------------------------------


@router.post("/dispatch", response_model=JarvisDispatchResponse)
async def jarvis_dispatch(
    request: JarvisDispatchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> JarvisDispatchResponse:
    """Dispatch agent task from Jarvis.

    Triggers immediate execution of an agent task and returns run/thread IDs
    for tracking. Jarvis can then listen to the SSE stream for updates.

    Args:
        request: Dispatch request with agent_id and optional task override
        db: Database session
        current_user: Authenticated user (Jarvis service account)

    Returns:
        JarvisDispatchResponse with run and thread IDs

    Raises:
        404: Agent not found
        409: Agent already running
        500: Execution error
    """
    # Get agent
    agent = crud.get_agent(db, request.agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {request.agent_id} not found",
        )

    # Optionally override task instructions
    original_task = agent.task_instructions
    if request.task_override:
        agent.task_instructions = request.task_override

    try:
        # Execute agent task (creates thread and run)
        thread = await execute_agent_task(db, agent, thread_type="manual")

        # Get the created run
        run = db.query(crud.models.AgentRun).filter(
            crud.models.AgentRun.thread_id == thread.id
        ).order_by(crud.models.AgentRun.created_at.desc()).first()

        if not run:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create agent run",
            )

        logger.info(f"Jarvis dispatched agent {agent.id} (run {run.id}, thread {thread.id})")

        return JarvisDispatchResponse(
            run_id=run.id,
            thread_id=thread.id,
            status=run.status.value if hasattr(run.status, "value") else str(run.status),
            agent_name=agent.name,
        )

    except ValueError as e:
        # Agent already running or validation error
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Jarvis dispatch failed for agent {agent.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to dispatch agent: {str(e)}",
        )
    finally:
        # Restore original task instructions if overridden
        if request.task_override:
            agent.task_instructions = original_task
            db.add(agent)
            db.commit()


# ---------------------------------------------------------------------------
# SSE Events Endpoint
# ---------------------------------------------------------------------------


async def _jarvis_event_generator(current_user):
    """Generate SSE events for Jarvis.

    Subscribes to the event bus and yields agent/run update events.
    Runs until the client disconnects.
    """
    # Create asyncio queue for this connection
    queue = asyncio.Queue()

    # Subscribe to relevant events
    def event_handler(event):
        """Handle event and put into queue."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Jarvis SSE queue full, dropping event")

    # Subscribe to agent and run events
    event_bus.subscribe(EventType.AGENT_UPDATED, event_handler)
    event_bus.subscribe(EventType.RUN_CREATED, event_handler)
    event_bus.subscribe(EventType.RUN_UPDATED, event_handler)

    try:
        # Send initial connection event
        import json
        yield {
            "event": "connected",
            "data": json.dumps({"message": "Jarvis SSE stream connected"}),
        }

        # Stream events
        while True:
            try:
                # Wait for event with timeout to allow periodic heartbeats
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                # Format event for SSE
                event_type = event.get("type", "unknown")
                event_data = {
                    "type": event_type,
                    "payload": event.get("payload", {}),
                    "timestamp": event.get("timestamp"),
                }

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data),
                }

            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"timestamp": asyncio.get_event_loop().time()}),
                }

    except asyncio.CancelledError:
        # Client disconnected
        logger.info("Jarvis SSE stream disconnected")
    finally:
        # Unsubscribe from events
        event_bus.unsubscribe(EventType.AGENT_UPDATED, event_handler)
        event_bus.unsubscribe(EventType.RUN_CREATED, event_handler)
        event_bus.unsubscribe(EventType.RUN_UPDATED, event_handler)


@router.get("/events")
async def jarvis_events(
    current_user=Depends(get_current_user),
) -> EventSourceResponse:
    """Server-Sent Events stream for Jarvis.

    Provides real-time updates for agent and run events. Jarvis listens to this
    stream to update the Task Inbox UI without polling.

    Event types:
    - connected: Initial connection confirmation
    - heartbeat: Keep-alive ping every 30 seconds
    - agent_updated: Agent status or configuration changed
    - run_created: New agent run started
    - run_updated: Agent run status changed (running → success/failed)

    Args:
        current_user: Authenticated user (Jarvis service account)

    Returns:
        EventSourceResponse streaming SSE events
    """
    return EventSourceResponse(_jarvis_event_generator(current_user))
