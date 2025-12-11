"""Jarvis Integration API Router.

Provides endpoints for Jarvis (voice/text UI) to interact with Zerg backend:
- Authentication: Device secret → JWT token
- Agent listing: Get available agents with schedules
- Run history: Recent agent executions with summaries
- Dispatch: Trigger agent tasks from Jarvis
- Events: SSE stream for real-time updates
"""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Dict, List, Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Agent, AgentRun
from zerg.services.task_runner import execute_agent_task
from zerg.services.supervisor_context import get_next_seq, reset_seq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jarvis", tags=["jarvis"])
JARVIS_SESSION_COOKIE = "jarvis_session"

# Track running supervisor tasks so we can cancel them on demand
_supervisor_tasks: Dict[int, asyncio.Task] = {}
_supervisor_tasks_lock = asyncio.Lock()


async def _register_supervisor_task(run_id: int, task: asyncio.Task) -> None:
    """Store the running supervisor task for cancellation."""
    async with _supervisor_tasks_lock:
        _supervisor_tasks[run_id] = task


async def _pop_supervisor_task(run_id: int) -> Optional[asyncio.Task]:
    """Remove and return the supervisor task for a run."""
    async with _supervisor_tasks_lock:
        return _supervisor_tasks.pop(run_id, None)


async def _cancel_supervisor_task(run_id: int) -> bool:
    """Attempt to cancel a running supervisor task.

    Returns:
        bool: True if a task was found (cancellation requested), False otherwise.
    """
    async with _supervisor_tasks_lock:
        task = _supervisor_tasks.get(run_id)

    if not task or task.done():
        return False

    task.cancel()
    try:
        # Give the task a moment to process cancellation
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        # Task is taking longer to cooperate; leave it cancelled in the background
        pass
    except asyncio.CancelledError:
        pass

    return True


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class JarvisAuthRequest(BaseModel):
    """Jarvis authentication request with device secret."""

    device_secret: str = Field(..., description="Device secret for Jarvis authentication")


class JarvisAuthResponse(BaseModel):
    """Jarvis authentication response metadata."""

    session_expires_in: int = Field(..., description="Session expiry window in seconds")
    session_cookie_name: str = Field(..., description="Name of session cookie storing Jarvis session")


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


class JarvisSupervisorRequest(BaseModel):
    """Request to dispatch a task to the supervisor agent."""

    task: str = Field(..., description="Natural language task for the supervisor")
    context: Optional[dict] = Field(
        None,
        description="Optional context including conversation_id and previous_messages",
    )
    preferences: Optional[dict] = Field(
        None,
        description="Optional preferences like verbosity and notify_on_complete",
    )


class JarvisSupervisorResponse(BaseModel):
    """Response from supervisor dispatch."""

    run_id: int = Field(..., description="Supervisor run ID for tracking")
    thread_id: int = Field(..., description="Supervisor thread ID (long-lived)")
    status: str = Field(..., description="Initial run status")
    stream_url: str = Field(..., description="SSE stream URL for progress updates")


class JarvisCancelResponse(BaseModel):
    """Response from supervisor cancellation."""

    run_id: int = Field(..., description="The cancelled run ID")
    status: str = Field(..., description="Run status after cancellation")
    message: str = Field(..., description="Human-readable status message")


# ---------------------------------------------------------------------------
# Authentication Endpoint
# ---------------------------------------------------------------------------


@router.post("/auth", response_model=JarvisAuthResponse)
def jarvis_auth(
    request: JarvisAuthRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> JarvisAuthResponse:
    """Authenticate Jarvis device and establish an authenticated session.

    Validates the device secret against environment configuration and issues
    a short-lived session cookie that Jarvis can use for subsequent API calls.

    Args:
        request: Contains device_secret for authentication
        db: Database session

    Returns:
        JarvisAuthResponse with session metadata

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

    jarvis_email = "jarvis@swarm.local"
    jarvis_user = crud.get_user_by_email(db, jarvis_email)
    if jarvis_user is None:
        jarvis_user = crud.create_user(
            db,
            email=jarvis_email,
            provider="jarvis",
            role="ADMIN",
        )
        jarvis_user.display_name = "Jarvis Assistant"
        db.add(jarvis_user)
        db.commit()
        db.refresh(jarvis_user)
    elif jarvis_user.display_name != "Jarvis Assistant":
        jarvis_user.display_name = "Jarvis Assistant"
        db.add(jarvis_user)
        db.commit()
        db.refresh(jarvis_user)

    # Issue JWT token for Jarvis (longer expiry for device auth)
    # Import the token issuing function from auth router
    from zerg.routers.auth import _issue_access_token

    # Create token with jarvis scope - expires in 7 days for device auth
    token_expiry_seconds = 60 * 60 * 24 * 7  # 7 days
    token = _issue_access_token(
        user_id=jarvis_user.id,
        email=jarvis_user.email,
        display_name="Jarvis",
        expires_delta=timedelta(seconds=token_expiry_seconds),
    )

    logger.info("Issued JWT token for Jarvis device")

    cookie_secure = False
    environment_value = (settings.environment or "").strip().lower()
    if environment_value and environment_value not in {"development", "dev", "local"} and not settings.testing:
        cookie_secure = True

    cookie_expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_expiry_seconds)
    response.set_cookie(
        key=JARVIS_SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
        max_age=token_expiry_seconds,
        expires=cookie_expires_at,
        path="/api/jarvis",
    )

    return JarvisAuthResponse(
        session_expires_in=token_expiry_seconds,
        session_cookie_name=JARVIS_SESSION_COOKIE,
    )


# ---------------------------------------------------------------------------
# Authentication Dependency
# ---------------------------------------------------------------------------


def get_current_jarvis_user(
    request: Request,
    db: Session = Depends(get_db),
):
    """Resolve the Jarvis session from HttpOnly session cookie.

    This is the unified authentication path for all Jarvis endpoints.
    Clients must call /api/jarvis/auth to receive the session cookie.
    """
    from zerg.dependencies.auth import AUTH_DISABLED
    from zerg.dependencies.auth import _get_strategy

    # Cookie-based authentication - the unified production path
    cookie_token = request.cookies.get(JARVIS_SESSION_COOKIE)
    if cookie_token:
        user = _get_strategy().validate_ws_token(cookie_token, db)
        if user is not None:
            return user

    # Dev-only bypass (global auth system fallback)
    if AUTH_DISABLED:
        return _get_strategy().get_current_user(request, db)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated - session cookie required",
    )


# ---------------------------------------------------------------------------
# Agent Listing Endpoint
# ---------------------------------------------------------------------------


@router.get("/agents", response_model=List[JarvisAgentSummary])
def list_jarvis_agents(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
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

    query = db.query(AgentRun)

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

# ---------------------------------------------------------------------------
# Dispatch Endpoint
# ---------------------------------------------------------------------------


@router.post("/dispatch", response_model=JarvisDispatchResponse)
async def jarvis_dispatch(
    request: JarvisDispatchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
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
        run = (
            db.query(AgentRun)
            .filter(AgentRun.thread_id == thread.id)
            .order_by(AgentRun.created_at.desc())
            .first()
        )

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
# Supervisor Endpoint (Super Siri Architecture)
# ---------------------------------------------------------------------------


@router.post("/supervisor", response_model=JarvisSupervisorResponse)
async def jarvis_supervisor(
    request: JarvisSupervisorRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisSupervisorResponse:
    """Dispatch a task to the supervisor agent.

    The supervisor is the "one brain" that coordinates workers and maintains
    long-term context. Each user has a single supervisor thread that persists
    across sessions.

    This endpoint:
    1. Finds or creates the user's supervisor thread (idempotent)
    2. Creates a new run attached to that thread
    3. Kicks off supervisor execution in the background
    4. Returns immediately with run_id and stream_url

    Args:
        request: Task and optional context/preferences
        background_tasks: FastAPI background tasks
        db: Database session
        current_user: Authenticated user

    Returns:
        JarvisSupervisorResponse with run_id, thread_id, and stream_url

    Example:
        POST /api/jarvis/supervisor
        {"task": "Check my server health"}

        Response:
        {
            "run_id": 456,
            "thread_id": 789,
            "status": "running",
            "stream_url": "/api/jarvis/supervisor/events?run_id=456"
        }
    """
    from zerg.services.supervisor_service import SupervisorService

    supervisor_service = SupervisorService(db)

    # Get or create supervisor components (idempotent)
    agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
    thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

    # Create run record (marks as running)
    from zerg.models.enums import RunStatus

    from zerg.models.enums import RunTrigger

    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    logger.info(
        f"Jarvis supervisor: created run {run.id} for user {current_user.id}, "
        f"task: {request.task[:50]}..."
    )

    # Start supervisor execution in background
    # We use asyncio.create_task directly since we're in an async context
    # and want the task to continue after the response is sent
    async def run_supervisor_background(owner_id: int, task: str, run_id: int):
        """Execute supervisor in background."""
        from zerg.database import db_session
        from zerg.services.supervisor_service import SupervisorService

        try:
            with db_session() as bg_db:
                service = SupervisorService(bg_db)
                # Run supervisor - pass run_id to avoid duplicate run creation
                await service.run_supervisor(
                    owner_id=owner_id,
                    task=task,
                    run_id=run_id,  # Use the run created in the endpoint
                    timeout=60,
                )
        except Exception as e:
            logger.exception(f"Background supervisor execution failed for run {run_id}: {e}")
        finally:
            await _pop_supervisor_task(run_id)

    # Create background task - runs independently of the request
    task_handle = asyncio.create_task(
        run_supervisor_background(current_user.id, request.task, run.id)
    )
    await _register_supervisor_task(run.id, task_handle)

    return JarvisSupervisorResponse(
        run_id=run.id,
        thread_id=thread.id,
        status="running",
        stream_url=f"/api/jarvis/supervisor/events?run_id={run.id}",
    )


# ---------------------------------------------------------------------------
# Supervisor SSE Events Endpoint
# ---------------------------------------------------------------------------


async def _supervisor_event_generator(run_id: int, owner_id: int):
    """Generate SSE events for a specific supervisor run.

    Subscribes to supervisor and worker events filtered by run_id/owner_id.
    All events include a monotonically increasing `seq` for idempotent reconnect handling.

    Args:
        run_id: The supervisor run ID to track
        owner_id: Owner ID for security filtering
    """
    queue: asyncio.Queue = asyncio.Queue()
    pending_workers = 0
    supervisor_done = False

    async def event_handler(event):
        """Filter and queue relevant events."""
        # Security: only emit events for this owner
        if event.get("owner_id") != owner_id:
            return

        # For supervisor events, filter by run_id
        if "run_id" in event and event.get("run_id") != run_id:
            return

        # Tool events MUST have run_id to prevent leaking across runs
        event_type = event.get("event_type") or event.get("type")
        if event_type in ("worker_tool_started", "worker_tool_completed", "worker_tool_failed"):
            if "run_id" not in event:
                logger.warning(f"Tool event missing run_id, dropping: {event_type}")
                return

        await queue.put(event)

    # Subscribe to supervisor/worker events
    event_bus.subscribe(EventType.SUPERVISOR_STARTED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_THINKING, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
    event_bus.subscribe(EventType.WORKER_SPAWNED, event_handler)
    event_bus.subscribe(EventType.WORKER_STARTED, event_handler)
    event_bus.subscribe(EventType.WORKER_COMPLETE, event_handler)
    event_bus.subscribe(EventType.WORKER_SUMMARY_READY, event_handler)
    event_bus.subscribe(EventType.ERROR, event_handler)
    # Subscribe to worker tool events (Phase 2: Activity Ticker)
    event_bus.subscribe(EventType.WORKER_TOOL_STARTED, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_COMPLETED, event_handler)
    event_bus.subscribe(EventType.WORKER_TOOL_FAILED, event_handler)

    try:
        # Send initial connection event with seq
        yield {
            "event": "connected",
            "data": json.dumps({
                "message": "Supervisor SSE stream connected",
                "run_id": run_id,
                "seq": get_next_seq(run_id),
            }),
        }

        # Stream events until supervisor completes or errors
        complete = False
        while not complete:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                # Determine event type
                event_type = event.get("event_type") or event.get("type") or "event"

                # Track worker lifecycle so we don't close the stream until workers finish
                if event_type == "worker_spawned":
                    pending_workers += 1
                elif event_type == "worker_complete" and pending_workers > 0:
                    pending_workers -= 1
                elif event_type == "worker_summary_ready" and pending_workers > 0:
                    # In rare cases worker_complete may be dropped; treat summary_ready as completion
                    pending_workers -= 1
                elif event_type == "supervisor_complete":
                    supervisor_done = True
                elif event_type == "error":
                    complete = True

                # Close once supervisor is done AND all workers for this run have finished
                if supervisor_done and pending_workers == 0:
                    complete = True

                # Format payload (remove internal fields)
                payload = {
                    k: v
                    for k, v in event.items()
                    if k not in {"event_type", "type", "owner_id"}
                }

                # Add monotonically increasing seq for idempotent reconnect handling
                seq = get_next_seq(run_id)

                yield {
                    "event": event_type,
                    "data": json.dumps({
                        "type": event_type,
                        "payload": payload,
                        "seq": seq,
                        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }),
                }

            except asyncio.TimeoutError:
                # Send heartbeat with seq
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({
                        "seq": get_next_seq(run_id),
                        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }),
                }

        # Run reached terminal state; clear sequence counter to avoid leaks
        reset_seq(run_id)

    except asyncio.CancelledError:
        logger.info(f"Supervisor SSE stream disconnected for run {run_id}")
    finally:
        # Unsubscribe from all events
        event_bus.unsubscribe(EventType.SUPERVISOR_STARTED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_THINKING, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.WORKER_SPAWNED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_STARTED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.WORKER_SUMMARY_READY, event_handler)
        event_bus.unsubscribe(EventType.ERROR, event_handler)
        # Unsubscribe from worker tool events
        event_bus.unsubscribe(EventType.WORKER_TOOL_STARTED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_COMPLETED, event_handler)
        event_bus.unsubscribe(EventType.WORKER_TOOL_FAILED, event_handler)


@router.get("/supervisor/events")
async def jarvis_supervisor_events(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> EventSourceResponse:
    """SSE stream for supervisor run progress.

    Provides real-time updates for a specific supervisor run including:
    - supervisor_started: Run has begun
    - supervisor_thinking: Supervisor is analyzing
    - worker_spawned: Worker job queued
    - worker_started: Worker execution began
    - worker_complete: Worker finished (success/failed)
    - worker_summary_ready: Worker summary extracted
    - supervisor_complete: Final result ready
    - error: Something went wrong
    - heartbeat: Keep-alive (every 30s)

    The stream automatically closes when the supervisor completes or errors.

    Args:
        run_id: The supervisor run ID to track
        db: Database session
        current_user: Authenticated user

    Returns:
        EventSourceResponse streaming supervisor events

    Raises:
        HTTPException 404: If run not found or doesn't belong to user
    """
    # Validate run exists and belongs to user
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    # Check ownership via the run's agent
    agent = db.query(Agent).filter(Agent.id == run.agent_id).first()
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",  # Don't reveal existence to other users
        )

    return EventSourceResponse(
        _supervisor_event_generator(run_id, current_user.id)
    )


# ---------------------------------------------------------------------------
# Supervisor Cancel Endpoint
# ---------------------------------------------------------------------------


@router.post("/supervisor/{run_id}/cancel", response_model=JarvisCancelResponse)
async def jarvis_supervisor_cancel(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisCancelResponse:
    """Cancel a running supervisor investigation.

    Marks the run as cancelled and emits a cancellation event to SSE subscribers.
    If the run is already complete, returns the current status without error.

    Args:
        run_id: The supervisor run ID to cancel
        db: Database session
        current_user: Authenticated user

    Returns:
        JarvisCancelResponse with run status

    Raises:
        HTTPException 404: If run not found or doesn't belong to user
    """
    from zerg.models.enums import RunStatus

    # Validate run exists
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    # Check ownership via the run's agent
    agent = db.query(Agent).filter(Agent.id == run.agent_id).first()
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",  # Don't reveal existence to other users
        )

    # Check if already complete
    terminal_statuses = {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}
    if run.status in terminal_statuses:
        return JarvisCancelResponse(
            run_id=run_id,
            status=run.status.value if hasattr(run.status, "value") else str(run.status),
            message="Run already completed",
        )

    # Mark as cancelled
    run.status = RunStatus.CANCELLED
    run.finished_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()

    # Attempt to cancel the running background task (best-effort)
    await _cancel_supervisor_task(run_id)

    logger.info(f"Supervisor run {run_id} cancelled by user {current_user.id}")

    # Emit cancellation event for SSE subscribers
    await event_bus.publish(
        EventType.SUPERVISOR_COMPLETE,
        {
            "event_type": "supervisor_complete",
            "run_id": run_id,
            "owner_id": current_user.id,
            "status": "cancelled",
            "message": "Investigation cancelled by user",
        },
    )

    # Reset sequence counter once final event is emitted
    reset_seq(run_id)

    return JarvisCancelResponse(
        run_id=run_id,
        status="cancelled",
        message="Investigation cancelled",
    )


# ---------------------------------------------------------------------------
# SSE Events Endpoint (General)
# ---------------------------------------------------------------------------


async def _jarvis_event_generator(_current_user):
    """Generate SSE events for Jarvis.

    Subscribes to the event bus and yields agent/run update events.
    Runs until the client disconnects.
    """
    # Create asyncio queue for this connection
    queue = asyncio.Queue()

    # Subscribe to relevant events
    async def event_handler(event):
        """Handle event and put into queue."""
        await queue.put(event)

    # Subscribe to agent and run events
    event_bus.subscribe(EventType.AGENT_UPDATED, event_handler)
    event_bus.subscribe(EventType.RUN_CREATED, event_handler)
    event_bus.subscribe(EventType.RUN_UPDATED, event_handler)

    try:
        # Send initial connection event
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
                event_type = event.get("event_type") or event.get("type") or "event"
                payload = {k: v for k, v in event.items() if k not in {"event_type", "type"}}
                event_data = {
                    "type": event_type,
                    "payload": payload,
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data),
                }

            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                heartbeat_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"timestamp": heartbeat_ts}),
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
    current_user=Depends(get_current_jarvis_user),
) -> EventSourceResponse:
    """Server-Sent Events stream for Jarvis.

    Provides real-time updates for agent and run events. Jarvis listens to this
    stream to update the Task Inbox UI without polling.

    Authentication:
    - HttpOnly session cookie set by `/api/jarvis/auth`
    - Development override: when `AUTH_DISABLED=1`, standard dev auth applies

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


# ---------------------------------------------------------------------------
# BFF Proxy Endpoints
# ---------------------------------------------------------------------------


class JarvisBootstrapResponse(BaseModel):
    """Bootstrap response with prompt, tools, and user context."""

    prompt: str = Field(..., description="Complete Jarvis system prompt")
    enabled_tools: List[dict] = Field(..., description="List of available tools")
    user_context: dict = Field(..., description="User context summary (safe subset)")


@router.get("/bootstrap", response_model=JarvisBootstrapResponse)
def jarvis_bootstrap(
    current_user=Depends(get_current_jarvis_user),
) -> JarvisBootstrapResponse:
    """Get Jarvis bootstrap configuration.

    Returns the complete system prompt (built from user context),
    list of enabled tools, and a safe subset of user context for display.

    This is the single source of truth for Jarvis configuration.
    """
    from zerg.prompts.composer import build_jarvis_prompt

    enabled_tools = [
        {"name": "get_current_location", "description": "Get current GPS location via Traccar"},
        {"name": "get_whoop_data", "description": "Get WHOOP health metrics"},
        {"name": "search_notes", "description": "Search notes via Obsidian"},
        {"name": "route_to_supervisor", "description": "Delegate complex tasks to supervisor agent"},
    ]

    prompt = build_jarvis_prompt(current_user, enabled_tools)

    ctx = current_user.context or {}
    user_context = {
        "display_name": ctx.get("display_name"),
        "role": ctx.get("role"),
        "location": ctx.get("location"),
        "servers": [{"name": s.get("name"), "purpose": s.get("purpose")} for s in ctx.get("servers", [])],
    }

    return JarvisBootstrapResponse(prompt=prompt, enabled_tools=enabled_tools, user_context=user_context)


@router.post("/session")
async def jarvis_session_proxy(request: Request):
    """Proxy OpenAI Realtime session token minting to jarvis-server."""
    import httpx

    settings = get_settings()
    jarvis_url = settings.jarvis_server_url
    if not jarvis_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jarvis server not configured")

    try:
        body = await request.body()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{jarvis_url}/session", content=body, headers={"Content-Type": "application/json"})
            return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type", "application/json"))
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Jarvis server timeout")
    except httpx.ConnectError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jarvis server unavailable")


@router.post("/tool")
async def jarvis_tool_proxy(request: Request):
    """Proxy tool execution to jarvis-server."""
    import httpx

    settings = get_settings()
    jarvis_url = settings.jarvis_server_url
    if not jarvis_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jarvis server not configured")

    try:
        body = await request.body()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{jarvis_url}/tool", content=body, headers={"Content-Type": "application/json"})
            return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type", "application/json"))
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Jarvis server timeout")
    except httpx.ConnectError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jarvis server unavailable")


@router.api_route("/sync/{path:path}", methods=["GET", "POST"])
async def jarvis_sync_proxy(request: Request, path: str):
    """Proxy sync operations to jarvis-server."""
    import httpx

    settings = get_settings()
    jarvis_url = settings.jarvis_server_url
    if not jarvis_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jarvis server not configured")

    try:
        body = await request.body() if request.method == "POST" else None
        async with httpx.AsyncClient(timeout=30.0) as client:
            target_url = f"{jarvis_url}/sync/{path}"
            if request.query_params:
                target_url += f"?{request.query_params}"

            if request.method == "POST":
                resp = await client.post(target_url, content=body, headers={"Content-Type": "application/json"})
            else:
                resp = await client.get(target_url)

            return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type", "application/json"))
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Jarvis server timeout")
    except httpx.ConnectError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jarvis server unavailable")
