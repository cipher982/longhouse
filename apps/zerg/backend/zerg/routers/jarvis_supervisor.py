"""Jarvis supervisor endpoints - dispatch, events, cancel."""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.routers.jarvis_auth import _is_tool_enabled
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.services.supervisor_context import get_next_seq
from zerg.services.supervisor_context import reset_seq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])

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

    logger.info(f"Jarvis supervisor: created run {run.id} for user {current_user.id}, task: {request.task[:50]}...")

    # Unit tests use a per-test ephemeral database that is torn down immediately after the request.
    # Spawning background tasks here can outlive the test and crash xdist workers during teardown.
    from zerg.config import get_settings

    if get_settings().testing:
        logger.debug("TESTING=1: skipping background supervisor execution for run %s", run.id)
        return JarvisSupervisorResponse(
            run_id=run.id,
            thread_id=thread.id,
            status="running",
            stream_url=f"/api/jarvis/supervisor/events?run_id={run.id}",
        )

    # Start supervisor execution in background
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
                    return_on_deferred=False,
                )
        except Exception as e:
            logger.exception(f"Background supervisor execution failed for run {run_id}: {e}")
        finally:
            await _pop_supervisor_task(run_id)

    # Create background task - runs independently of the request
    task_handle = asyncio.create_task(run_supervisor_background(current_user.id, request.task, run.id))
    await _register_supervisor_task(run.id, task_handle)

    return JarvisSupervisorResponse(
        run_id=run.id,
        thread_id=thread.id,
        status="running",
        stream_url=f"/api/jarvis/supervisor/events?run_id={run.id}",
    )


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
    event_bus.subscribe(EventType.SUPERVISOR_DEFERRED, event_handler)  # Timeout migration
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
            "data": json.dumps(
                {
                    "message": "Supervisor SSE stream connected",
                    "run_id": run_id,
                    "seq": get_next_seq(run_id),
                }
            ),
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
                elif event_type == "supervisor_deferred":
                    # v2.2: Timeout migration - supervisor deferred, close stream
                    complete = True
                elif event_type == "error":
                    complete = True

                # Close once supervisor is done AND all workers for this run have finished
                if supervisor_done and pending_workers == 0:
                    complete = True

                # Format payload (remove internal fields)
                payload = {k: v for k, v in event.items() if k not in {"event_type", "type", "owner_id"}}

                # Add monotonically increasing seq for idempotent reconnect handling
                seq = get_next_seq(run_id)

                yield {
                    "event": event_type,
                    "data": json.dumps(
                        {
                            "type": event_type,
                            "payload": payload,
                            "seq": seq,
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }
                    ),
                }

            except asyncio.TimeoutError:
                # Send heartbeat with seq
                yield {
                    "event": "heartbeat",
                    "data": json.dumps(
                        {
                            "seq": get_next_seq(run_id),
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }
                    ),
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
        event_bus.unsubscribe(EventType.SUPERVISOR_DEFERRED, event_handler)
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

    return EventSourceResponse(_supervisor_event_generator(run_id, current_user.id))


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
