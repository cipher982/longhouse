"""Jarvis internal endpoints for run continuation and background completion.

These endpoints are called internally by the backend (not exposed to public API)
to handle run continuation when a supervisor times out and workers complete.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import require_internal_call
from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/runs",
    tags=["internal"],
    dependencies=[Depends(require_internal_call)],
)


class WorkerCompletionPayload(BaseModel):
    """Payload for worker completion webhook."""

    job_id: int = Field(..., description="Worker job ID")
    worker_id: str = Field(..., description="Worker artifact ID")
    status: str = Field(..., description="Worker status: success or failed")
    result_summary: str | None = Field(None, description="Brief summary of worker result")
    error: str | None = Field(None, description="Error message if failed")


@router.post("/{run_id}/continue")
async def continue_run(
    run_id: int,
    payload: WorkerCompletionPayload,
    db: Session = Depends(get_db),
):
    """Continue a DEFERRED run when a worker completes.

    Called internally when a worker completes while the supervisor run was DEFERRED.
    This endpoint:
    1. Finds the DEFERRED run
    2. Injects the worker result as a tool message into the thread
    3. Creates a new continuation run to synthesize the final answer
    4. Emits events so connected SSE clients get notified

    Args:
        run_id: ID of the DEFERRED supervisor run
        payload: Worker completion data
        db: Database session

    Returns:
        Dict with continuation run info

    Raises:
        404: Run not found or not in DEFERRED status
        500: Error creating continuation
    """
    # Find the DEFERRED run
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    if run.status != RunStatus.DEFERRED:
        # If run already completed or failed, this is a no-op (idempotent)
        logger.info(f"Run {run_id} status is {run.status.value}, skipping continuation")
        return {
            "status": "skipped",
            "reason": f"Run is {run.status.value}, not DEFERRED",
            "run_id": run_id,
        }

    # Get the agent and thread
    agent = crud.get_agent(db, run.agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent {run.agent_id} not found",
        )

    thread = crud.get_thread(db, run.thread_id)
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Thread {run.thread_id} not found",
        )

    logger.info(f"Continuing run {run_id} after worker {payload.worker_id} completed with status {payload.status}")

    try:
        # Prepare result text for continuation
        result_text = payload.result_summary or f"Worker job {payload.job_id} completed"
        if payload.status == "failed":
            result_text = f"Worker failed: {payload.error or 'Unknown error'}"

        # Use SupervisorService.run_continuation which handles:
        # 1. Injecting worker result as tool message
        # 2. Creating continuation run
        # 3. Running supervisor to synthesize final answer
        # 4. Emitting all necessary events
        from zerg.services.supervisor_service import SupervisorService

        supervisor = SupervisorService(db)
        result = await supervisor.run_continuation(
            original_run_id=run_id,
            job_id=payload.job_id,
            worker_id=payload.worker_id,
            result_summary=result_text,
        )

        return {
            "status": "continued",
            "run_id": run_id,
            "continuation_run_id": result.run_id,
            "result_status": result.status,
        }

    except Exception as e:
        logger.exception(f"Error continuing run {run_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to continue run: {str(e)}",
        )
