"""Jarvis supervisor endpoints - dispatch and cancel."""

import asyncio
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

from zerg.database import get_db
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.routers.jarvis_auth import _is_tool_enabled
from zerg.routers.jarvis_auth import get_current_jarvis_user
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
            "stream_url": "/api/stream/runs/456"
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
            stream_url=f"/api/stream/runs/{run.id}",
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
        stream_url=f"/api/stream/runs/{run.id}",
    )


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
