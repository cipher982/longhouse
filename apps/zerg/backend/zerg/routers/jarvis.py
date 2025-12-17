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
from datetime import timezone
from typing import Dict, List, Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
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
    """Deprecated: Jarvis now uses standard SaaS user authentication.

    Jarvis is treated as a normal client (like the dashboard). It authenticates
    using the same JWT bearer token as other frontend clients.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Deprecated: Jarvis uses standard user login (JWT bearer token).",
    )


# ---------------------------------------------------------------------------
# Authentication Dependency
# ---------------------------------------------------------------------------


def get_current_jarvis_user(
    request: Request,
    db: Session = Depends(get_db),
    token: str | None = Query(
        None,
        description="Optional JWT token (used by EventSource/SSE which can't send Authorization headers).",
    ),
):
    """Resolve the authenticated user for Jarvis endpoints.

    SaaS model: Jarvis is just another client UI and uses standard auth.

    - For normal fetch/XHR: use `Authorization: Bearer <token>`
    - For SSE/EventSource: pass `token=<jwt>` as a query param
    """
    from zerg.dependencies.auth import _get_strategy

    if token:
        user = _get_strategy().validate_ws_token(token, db)
        if user is not None:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    return _get_strategy().get_current_user(request, db)


def _is_tool_enabled(ctx: dict, tool_key: str) -> bool:
    tool_config = (ctx or {}).get("tools", {}) or {}
    return bool(tool_config.get(tool_key, True))


def _tool_key_from_mcp_call(name: str) -> str | None:
    if name.startswith("location."):
        return "location"
    if name.startswith("whoop."):
        return "whoop"
    if name.startswith("obsidian."):
        return "obsidian"
    return None


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
    # Multi-tenant SaaS: Jarvis shows only the logged-in user's agents.
    agents = crud.get_agents(db, owner_id=current_user.id)

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
    # Authorization: only owner or admin may dispatch an agent's task
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")

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

    # Server-side enforcement: respect user tool configuration.
    if not _is_tool_enabled(current_user.context or {}, "supervisor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tool disabled: supervisor",
        )

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
                    timeout=120,
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
    - Standard SaaS auth: `Authorization: Bearer <jwt>`
    - SSE fallback: `?token=<jwt>` query parameter (EventSource cannot send headers)
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
# Chat Endpoint (Text-Only via Supervisor)
# ---------------------------------------------------------------------------


class JarvisChatRequest(BaseModel):
    """Request for text chat with Supervisor."""

    message: str = Field(..., description="User message text")


async def _chat_stream_generator(run_id: int, owner_id: int, message: str):
    """Generate SSE events for chat streaming.

    Subscribes to supervisor events and streams assistant responses.
    The background task is started from within this generator to avoid race conditions.
    """
    queue: asyncio.Queue = asyncio.Queue()
    task_handle: Optional[asyncio.Task] = None

    async def event_handler(event):
        """Filter and queue relevant events."""
        # Security: only emit events for this owner
        if event.get("owner_id") != owner_id:
            return

        # Filter by run_id
        if "run_id" in event and event.get("run_id") != run_id:
            return

        await queue.put(event)

    async def run_supervisor_background():
        """Execute supervisor in background."""
        from zerg.database import db_session
        from zerg.services.supervisor_service import SupervisorService

        try:
            with db_session() as bg_db:
                service = SupervisorService(bg_db)
                await service.run_supervisor(
                    owner_id=owner_id,
                    task=message,
                    run_id=run_id,
                    timeout=120,
                )
        except Exception as e:
            logger.exception(f"Background supervisor execution failed for run {run_id}: {e}")
            # Emit error event so the stream knows to close
            await event_bus.publish(
                EventType.ERROR,
                {
                    "event_type": "error",
                    "run_id": run_id,
                    "owner_id": owner_id,
                    "error": str(e),
                },
            )
        finally:
            await _pop_supervisor_task(run_id)

    # Subscribe to supervisor events BEFORE starting background task
    event_bus.subscribe(EventType.SUPERVISOR_STARTED, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_THINKING, event_handler)
    event_bus.subscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
    event_bus.subscribe(EventType.ERROR, event_handler)

    try:
        # Send initial connection event
        yield {
            "event": "connected",
            "data": json.dumps({
                "message": "Chat stream connected",
                "run_id": run_id,
            }),
        }

        # NOW start the background task - after subscriptions are ready and connected event sent
        logger.info(f"Chat SSE: starting background supervisor for run {run_id}")
        task_handle = asyncio.create_task(run_supervisor_background())
        await _register_supervisor_task(run_id, task_handle)

        # Stream events until supervisor completes or errors
        complete = False
        while not complete:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                event_type = event.get("event_type") or event.get("type") or "event"
                logger.debug(f"Chat SSE: received event {event_type} for run {run_id}")

                # Check for completion
                if event_type in ("supervisor_complete", "error"):
                    complete = True

                # Format payload
                payload = {
                    k: v
                    for k, v in event.items()
                    if k not in {"event_type", "type", "owner_id"}
                }

                yield {
                    "event": event_type,
                    "data": json.dumps({
                        "type": event_type,
                        "payload": payload,
                        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }),
                }

            except asyncio.TimeoutError:
                # Send heartbeat
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({
                        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }),
                }

    except asyncio.CancelledError:
        logger.info(f"Chat SSE stream disconnected for run {run_id}")
        if task_handle and not task_handle.done():
            task_handle.cancel()
    finally:
        # Unsubscribe from all events
        event_bus.unsubscribe(EventType.SUPERVISOR_STARTED, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_THINKING, event_handler)
        event_bus.unsubscribe(EventType.SUPERVISOR_COMPLETE, event_handler)
        event_bus.unsubscribe(EventType.ERROR, event_handler)


@router.post("/chat")
async def jarvis_chat(
    request: JarvisChatRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> EventSourceResponse:
    """Text chat endpoint - streams responses from Supervisor.

    This endpoint provides a simpler alternative to /supervisor for text-only
    chat. It still uses the Supervisor under the hood but returns an SSE stream
    directly instead of requiring a separate connection.

    Args:
        request: Chat request with user message
        db: Database session
        current_user: Authenticated user

    Returns:
        EventSourceResponse streaming chat responses

    Example:
        POST /api/jarvis/chat
        {"message": "What's the weather?"}

        Streams SSE events:
        - supervisor_started: Chat processing started
        - supervisor_thinking: Supervisor analyzing
        - supervisor_complete: Final response with result
    """
    from zerg.services.supervisor_service import SupervisorService

    supervisor_service = SupervisorService(db)

    # Server-side enforcement: respect user tool configuration
    if not _is_tool_enabled(current_user.context or {}, "supervisor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tool disabled: supervisor",
        )

    # Get or create supervisor components
    agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
    thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

    # Create run record
    from zerg.models.enums import RunStatus, RunTrigger

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
        f"Jarvis chat: created run {run.id} for user {current_user.id}, "
        f"message: {request.message[:50]}..."
    )

    # Return SSE stream - background task is started inside the generator
    # to avoid race conditions with event subscriptions
    return EventSourceResponse(
        _chat_stream_generator(run.id, current_user.id, request.message)
    )


# ---------------------------------------------------------------------------
# History Endpoint
# ---------------------------------------------------------------------------


class JarvisChatMessage(BaseModel):
    """Single chat message in history."""

    role: str = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")


class JarvisHistoryResponse(BaseModel):
    """Chat history response."""

    messages: List[JarvisChatMessage] = Field(..., description="List of messages")
    total: int = Field(..., description="Total message count")


@router.get("/history", response_model=JarvisHistoryResponse)
def jarvis_history(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisHistoryResponse:
    """Get conversation history from Supervisor thread.

    Returns paginated message history from the user's supervisor thread.
    Only includes user and assistant messages (filters out system messages).

    Args:
        limit: Maximum number of messages to return (default 50)
        offset: Number of messages to skip (default 0)
        db: Database session
        current_user: Authenticated user

    Returns:
        JarvisHistoryResponse with messages and total count
    """
    from zerg.services.supervisor_service import SupervisorService
    from zerg.models.models import ThreadMessage

    supervisor_service = SupervisorService(db)

    # Get supervisor thread (creates if doesn't exist)
    agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
    thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

    # Query messages from thread (user and assistant only)
    query = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role.in_(["user", "assistant"]),
        )
        .order_by(ThreadMessage.sent_at.asc())
    )

    # Get total count
    total = query.count()

    # Get paginated messages
    messages = query.offset(offset).limit(limit).all()

    # Convert to response format
    chat_messages = [
        JarvisChatMessage(
            role=msg.role,
            content=msg.content,
            timestamp=msg.sent_at,
        )
        for msg in messages
    ]

    logger.info(
        f"Jarvis history: returned {len(chat_messages)} messages "
        f"(offset={offset}, limit={limit}, total={total}) for user {current_user.id}"
    )

    return JarvisHistoryResponse(
        messages=chat_messages,
        total=total,
    )


@router.delete("/history", status_code=status.HTTP_204_NO_CONTENT)
def jarvis_clear_history(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> None:
    """Clear conversation history by creating a new Supervisor thread.

    This creates a fresh thread for the user's Supervisor agent, effectively
    clearing all conversation history. The old thread is preserved in the
    database but no longer used.

    Args:
        db: Database session
        current_user: Authenticated user
    """
    from zerg.services.supervisor_service import SupervisorService
    from zerg.models.models import Agent

    supervisor_service = SupervisorService(db)

    # Get supervisor agent and current thread
    agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
    old_thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

    # Delete all messages from the thread (keeps thread, clears history)
    from zerg.models.models import ThreadMessage

    deleted_count = db.query(ThreadMessage).filter(
        ThreadMessage.thread_id == old_thread.id,
    ).delete()

    db.commit()

    logger.info(f"Jarvis history cleared: deleted {deleted_count} messages from thread {old_thread.id} for user {current_user.id}")


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

    # Define all available tools
    AVAILABLE_TOOLS = {
        "location": {"name": "get_current_location", "description": "Get current GPS location via Traccar"},
        "whoop": {"name": "get_whoop_data", "description": "Get WHOOP health metrics"},
        "obsidian": {"name": "search_notes", "description": "Search notes via Obsidian"},
        "supervisor": {"name": "route_to_supervisor", "description": "Delegate complex tasks to supervisor agent"},
    }

    # Get tool configuration from user context (default all enabled)
    ctx = current_user.context or {}
    tool_config = ctx.get("tools", {})

    # Build enabled tools list based on user configuration
    enabled_tools = []
    for tool_key, tool_def in AVAILABLE_TOOLS.items():
        # Default to enabled if not explicitly configured
        if _is_tool_enabled(ctx, tool_key):
            enabled_tools.append(tool_def)

    prompt = build_jarvis_prompt(current_user, enabled_tools)

    user_context = {
        "display_name": ctx.get("display_name"),
        "role": ctx.get("role"),
        "location": ctx.get("location"),
        "servers": [{"name": s.get("name"), "purpose": s.get("purpose")} for s in ctx.get("servers", [])],
    }

    return JarvisBootstrapResponse(prompt=prompt, enabled_tools=enabled_tools, user_context=user_context)


@router.get("/session")
async def jarvis_session_get(
    request: Request,
    current_user=Depends(get_current_jarvis_user),
):
    """Mint an ephemeral OpenAI Realtime session token.

    Directly calls OpenAI's API - no separate jarvis-server needed.
    """
    import httpx

    from zerg.services.openai_realtime import mint_realtime_session_token

    try:
        result = await mint_realtime_session_token()
        return result
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="OpenAI API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI API error: {e.response.text}")


@router.post("/session")
async def jarvis_session_post(
    request: Request,
    current_user=Depends(get_current_jarvis_user),
):
    """Backwards compatibility: some clients may still POST."""
    import httpx

    from zerg.services.openai_realtime import mint_realtime_session_token

    logger.debug("Jarvis session: received POST /session; handling directly")
    try:
        result = await mint_realtime_session_token()
        return result
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="OpenAI API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI API error: {e.response.text}")


@router.post("/conversation/title")
async def jarvis_conversation_title(
    request: Request,
    current_user=Depends(get_current_jarvis_user),
):
    """Generate a conversation title using OpenAI.

    Directly calls OpenAI's API - no separate jarvis-server needed.
    """
    import httpx

    from zerg.services.title_generator import generate_conversation_title

    try:
        body = await request.json()
        messages = body.get("messages", [])

        title = await generate_conversation_title(messages)
        if title:
            return {"title": title}
        else:
            return {"title": None, "error": "Could not generate title"}
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="OpenAI API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI API error: {e.response.text}")
