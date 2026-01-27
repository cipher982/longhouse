"""Oikos run dispatch endpoints - create and cancel."""

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
from zerg.models.models import Fiche
from zerg.models.models import Run
from zerg.routers.oikos_auth import _is_tool_enabled
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.services.oikos_context import reset_seq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["oikos"])

# Track running oikos tasks so we can cancel them on demand
_oikos_tasks: Dict[int, asyncio.Task] = {}
_oikos_tasks_lock = asyncio.Lock()


async def _register_oikos_task(run_id: int, task: asyncio.Task) -> None:
    """Store the running oikos task for cancellation."""
    async with _oikos_tasks_lock:
        _oikos_tasks[run_id] = task


async def _pop_oikos_task(run_id: int) -> Optional[asyncio.Task]:
    """Remove and return the oikos task for a run."""
    async with _oikos_tasks_lock:
        return _oikos_tasks.pop(run_id, None)


async def _cancel_oikos_task(run_id: int) -> bool:
    """Attempt to cancel a running oikos task.

    Returns:
        bool: True if a task was found (cancellation requested), False otherwise.
    """
    async with _oikos_tasks_lock:
        task = _oikos_tasks.get(run_id)

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


class OikosRunRequest(BaseModel):
    """Request to dispatch a task to the oikos fiche."""

    task: str = Field(..., description="Natural language task for the oikos")
    context: Optional[dict] = Field(
        None,
        description="Optional context including conversation_id and previous_messages",
    )
    preferences: Optional[dict] = Field(
        None,
        description="Optional preferences like verbosity and notify_on_complete",
    )


class OikosRunResponse(BaseModel):
    """Response from oikos dispatch."""

    run_id: int = Field(..., description="Oikos run ID for tracking")
    thread_id: int = Field(..., description="Oikos thread ID (long-lived)")
    status: str = Field(..., description="Initial run status")
    stream_url: str = Field(..., description="SSE stream URL for progress updates")


class OikosRunCancelResponse(BaseModel):
    """Response from oikos cancellation."""

    run_id: int = Field(..., description="The cancelled run ID")
    status: str = Field(..., description="Run status after cancellation")
    message: str = Field(..., description="Human-readable status message")


@router.post("/run", response_model=OikosRunResponse)
async def oikos_run(
    request: OikosRunRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> OikosRunResponse:
    """Dispatch a task to the oikos fiche.

    The oikos is the "one brain" that coordinates commis and maintains
    long-term context. Each user has a single oikos thread that persists
    across sessions.

    This endpoint:
    1. Finds or creates the user's oikos thread (idempotent)
    2. Creates a new run attached to that thread
    3. Kicks off oikos execution in the background
    4. Returns immediately with run_id and stream_url

    Args:
        request: Task and optional context/preferences
        db: Database session
        current_user: Authenticated user

    Returns:
        OikosRunResponse with run_id, thread_id, and stream_url

    Example:
        POST /api/oikos/run
        {"task": "Check my server health"}

        Response:
        {
            "run_id": 456,
            "thread_id": 789,
            "status": "running",
            "stream_url": "/api/stream/runs/456"
        }
    """
    from zerg.services.oikos_service import OikosService

    oikos_service = OikosService(db)

    # Server-side enforcement: respect user tool configuration.
    if not _is_tool_enabled(current_user.context or {}, "oikos"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tool disabled: oikos",
        )

    # Get or create oikos components (idempotent)
    fiche = oikos_service.get_or_create_oikos_fiche(current_user.id)
    thread = oikos_service.get_or_create_oikos_thread(current_user.id, fiche)

    # Create run record (marks as running)
    from zerg.models.enums import RunStatus
    from zerg.models.enums import RunTrigger

    run = Run(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    logger.info(f"Oikos run: created run {run.id} for user {current_user.id}, task: {request.task[:50]}...")

    # Unit tests use a per-test ephemeral database that is torn down immediately after the request.
    # Spawning background tasks here can outlive the test and crash xdist commis during teardown.
    from zerg.config import get_settings

    if get_settings().testing:
        logger.debug("TESTING=1: skipping background oikos execution for run %s", run.id)
        return OikosRunResponse(
            run_id=run.id,
            thread_id=thread.id,
            status="running",
            stream_url=f"/api/stream/runs/{run.id}",
        )

    # Start oikos execution in background
    async def run_oikos_background(owner_id: int, task: str, run_id: int):
        """Execute oikos in background."""
        from zerg.database import db_session
        from zerg.services.oikos_service import OikosService

        try:
            with db_session() as bg_db:
                service = OikosService(bg_db)
                # Run oikos - pass run_id to avoid duplicate run creation
                await service.run_oikos(
                    owner_id=owner_id,
                    task=task,
                    run_id=run_id,  # Use the run created in the endpoint
                    timeout=120,
                    return_on_deferred=False,
                )
        except Exception as e:
            logger.exception(f"Background oikos execution failed for run {run_id}: {e}")
        finally:
            await _pop_oikos_task(run_id)

    # Create background task - runs independently of the request
    task_handle = asyncio.create_task(run_oikos_background(current_user.id, request.task, run.id))
    await _register_oikos_task(run.id, task_handle)

    return OikosRunResponse(
        run_id=run.id,
        thread_id=thread.id,
        status="running",
        stream_url=f"/api/stream/runs/{run.id}",
    )


@router.post("/run/{run_id}/cancel", response_model=OikosRunCancelResponse)
async def oikos_run_cancel(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> OikosRunCancelResponse:
    """Cancel a running oikos investigation.

    Marks the run as cancelled and emits a cancellation event to SSE subscribers.
    If the run is already complete, returns the current status without error.

    Args:
        run_id: The oikos run ID to cancel
        db: Database session
        current_user: Authenticated user

    Returns:
        OikosRunCancelResponse with run status

    Raises:
        HTTPException 404: If run not found or doesn't belong to user
    """
    from zerg.models.enums import RunStatus

    # Validate run exists
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    # Check ownership via the run's fiche
    fiche = db.query(Fiche).filter(Fiche.id == run.fiche_id).first()
    if not fiche or fiche.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",  # Don't reveal existence to other users
        )

    # Check if already complete
    terminal_statuses = {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}
    if run.status in terminal_statuses:
        return OikosRunCancelResponse(
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
    await _cancel_oikos_task(run_id)

    logger.info(f"Oikos run {run_id} cancelled by user {current_user.id}")

    # Emit cancellation event for SSE subscribers
    await event_bus.publish(
        EventType.OIKOS_COMPLETE,
        {
            "event_type": "oikos_complete",
            "run_id": run_id,
            "owner_id": current_user.id,
            "status": "cancelled",
            "message": "Investigation cancelled by user",
        },
    )

    # Reset sequence counter once final event is emitted
    reset_seq(run_id)

    return OikosRunCancelResponse(
        run_id=run_id,
        status="cancelled",
        message="Investigation cancelled",
    )
