from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import User
from zerg.schemas.workflow import ExecutionStatusResponse, ExecutionLogsResponse
from zerg.services.workflow_engine import workflow_engine
from zerg.services.workflow_scheduler import workflow_scheduler
from zerg.utils.time import utc_now_naive

router = APIRouter(
    prefix="/workflow-executions",
    tags=["workflow-executions"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CancelPayload(BaseModel):
    reason: str = Field(..., max_length=500)


class ScheduleWorkflowPayload(BaseModel):
    cron_expression: str = Field(..., min_length=1, max_length=100)
    trigger_config: dict = Field(default_factory=dict)


@router.post("/by-workflow/{workflow_id}/reserve", response_model=ExecutionStatusResponse)
async def reserve_workflow_execution(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Reserve an execution ID for a workflow without starting execution.
    This allows the frontend to subscribe to WebSocket messages before execution starts.
    """
    workflow = crud.get_workflow(db, workflow_id)
    if not workflow or workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Create execution record with "waiting" phase (reserved for execution)
    execution = crud.create_workflow_execution(db, workflow_id=workflow_id, phase="waiting", triggered_by="manual")

    return ExecutionStatusResponse(
        execution_id=execution.id,
        phase="waiting",
        result=None
    )


@router.post("/by-workflow/{workflow_id}/start", response_model=ExecutionStatusResponse)
async def start_workflow_execution(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Start a new execution of a workflow using LangGraph engine.
    Non-blocking: returns immediately with phase=running.
    """
    workflow = crud.get_workflow(db, workflow_id)
    if not workflow or workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Execute workflow with LangGraph engine (non-blocking)
    try:
        execution_id = await workflow_engine.execute_workflow(workflow_id)
        return ExecutionStatusResponse(
            execution_id=execution_id,
            phase="running",
            result=None
        )
    except Exception as e:
        # Log the full error for debugging
        import logging
        logger = logging.getLogger(__name__)
        logger.exception(f"Failed to start workflow {workflow_id}")

        # Return user-friendly error message
        error_msg = str(e)
        if "InvalidUpdateError" in error_msg:
            error_msg = "Workflow execution failed: concurrent state update error. Check backend logs for details."
        elif "execution_id not found" in error_msg:
            error_msg = "Workflow execution failed: missing execution context. This is a configuration error."

        raise HTTPException(
            status_code=500,
            detail=f"Failed to start workflow: {error_msg}"
        )


@router.post("/executions/{execution_id}/start", response_model=ExecutionStatusResponse)
async def start_reserved_execution(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Start a previously reserved execution.
    """
    execution = crud.get_workflow_execution(db, execution_id)
    if not execution or execution.workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.phase != "waiting":
        raise HTTPException(status_code=400, detail="Execution is not in waiting state")

    # Update phase to running using ExecutionStateMachine
    from zerg.services.execution_state import ExecutionStateMachine

    ExecutionStateMachine.mark_running(execution)
    execution.started_at = datetime.now(timezone.utc)
    db.commit()

    # Start execution using the proper task tracking
    workflow_engine.start_workflow_in_background(execution.workflow_id, execution_id)

    return ExecutionStatusResponse(
        execution_id=execution_id,
        phase="running",
        result=None
    )


# Backward compatibility - DEPRECATED (place after specific routes to avoid conflicts)
@router.post("/{workflow_id}/start")
async def start_workflow_execution_deprecated(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    DEPRECATED: Use /by-workflow/{workflow_id}/start instead.
    Start a new execution of a workflow.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.warning(f"Using deprecated route POST /{workflow_id}/start - use /by-workflow/{workflow_id}/start")

    workflow = crud.get_workflow(db, workflow_id)
    if not workflow or workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Check for existing waiting execution first
    waiting = crud.get_waiting_execution_for_workflow(db, workflow_id)
    if waiting:
        logger.warning("Reuse reserved execution %s for legacy /start", waiting.id)
        # Return actual execution state instead of hard-coded values
        return {"execution_id": waiting.id, "phase": waiting.phase, "result": waiting.result}

    # Fall back to creating new execution
    return await start_workflow_execution(workflow_id, db, current_user)


@router.get("/{execution_id}/status", response_model=ExecutionStatusResponse)
def get_execution_status(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the status of a workflow execution.
    """
    execution = crud.get_workflow_execution(db, execution_id)
    if not execution or execution.workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Execution not found")
    return ExecutionStatusResponse(
        execution_id=execution.id,
        phase=execution.phase,
        result=execution.result
    )


@router.post("/{execution_id}/await")
async def await_execution_completion(
    execution_id: int,
    timeout: float = 30.0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Wait for a workflow execution to complete (synchronous).

    This endpoint blocks until the workflow completes or times out.
    Useful for testing and simple synchronous workflows.

    Args:
        execution_id: ID of the execution to wait for
        timeout: Maximum time to wait in seconds (default 30)

    Returns:
        Execution status when complete
    """
    execution = crud.get_workflow_execution(db, execution_id)
    if not execution or execution.workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Execution not found")

    # Wait for completion using the workflow engine
    completed = await workflow_engine.wait_for_completion(execution_id, timeout=timeout)

    if not completed:
        raise HTTPException(status_code=408, detail="Execution timed out")

    # Refresh execution from database
    db.refresh(execution)

    return {"execution_id": execution_id, "phase": execution.phase, "result": execution.result, "completed": True}


@router.get("/{execution_id}/logs", response_model=ExecutionLogsResponse)
def get_execution_logs(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the logs of a workflow execution.
    """
    execution = crud.get_workflow_execution(db, execution_id)
    if not execution or execution.workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Execution not found")
    return ExecutionLogsResponse(logs=execution.log or "")


@router.get("/history/{workflow_id}")
def get_execution_history(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the execution history of a workflow.
    """
    workflow = crud.get_workflow(db, workflow_id)
    if not workflow or workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return crud.get_workflow_executions(db, workflow_id)


@router.get("/{execution_id}/export")
def export_execution_data(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Export the data of a workflow execution.
    """
    execution = crud.get_workflow_execution(db, execution_id)
    if not execution or execution.workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Execution not found")
    return execution


# ---------------------------------------------------------------------------
# Cancellation endpoint
# ---------------------------------------------------------------------------


@router.patch("/{execution_id}/cancel", status_code=204)
def cancel_execution(
    *,
    execution_id: int,
    payload: CancelPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a running workflow execution as *cancelled*.

    The engine cooperatively checks the updated status before starting each
    new node and exits early. If the execution already finished the endpoint
    returns 409.
    """

    execution = crud.get_workflow_execution(db, execution_id)
    if execution is None or execution.workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.phase == "finished":
        raise HTTPException(status_code=409, detail="Execution already finished")

    from zerg.services.execution_state import ExecutionStateMachine

    ExecutionStateMachine.mark_cancelled(execution, reason=payload.reason)
    execution.finished_at = utc_now_naive()
    db.commit()

    # Emit EXECUTION_FINISHED event with cancelled status so UI updates
    from zerg.events import EventType  # local import to avoid cycles
    from zerg.events.publisher import publish_event_fire_and_forget

    payload_dict = {
        "execution_id": execution.id,
        "status": "cancelled",
        "error": payload.reason,
        "duration_ms": None,
        "event_type": EventType.EXECUTION_FINISHED,
    }

    publish_event_fire_and_forget(EventType.EXECUTION_FINISHED, payload_dict)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Workflow Scheduling endpoints
# ---------------------------------------------------------------------------


@router.post("/{workflow_id}/schedule")
async def schedule_workflow(
    workflow_id: int,
    payload: ScheduleWorkflowPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Schedule a workflow to run on a cron schedule.
    """
    workflow = crud.get_workflow(db, workflow_id)
    if not workflow or workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")

    success = await workflow_scheduler.schedule_workflow(
        workflow_id=workflow_id,
        cron_expression=payload.cron_expression,
        trigger_config=payload.trigger_config,
    )

    if not success:
        raise HTTPException(status_code=400, detail="Failed to schedule workflow")

    return {"status": "scheduled", "cron_expression": payload.cron_expression}


@router.delete("/{workflow_id}/schedule")
def unschedule_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Remove the schedule for a workflow.
    """
    workflow = crud.get_workflow(db, workflow_id)
    if not workflow or workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")

    success = workflow_scheduler.unschedule_workflow(workflow_id)

    if not success:
        raise HTTPException(status_code=404, detail="Workflow not scheduled")

    return {"status": "unscheduled"}


@router.get("/{workflow_id}/schedule")
def get_workflow_schedule(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the current schedule for a workflow.
    """
    workflow = crud.get_workflow(db, workflow_id)
    if not workflow or workflow.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")

    scheduled_workflows = workflow_scheduler.get_scheduled_workflows()

    if workflow_id in scheduled_workflows:
        schedule_info = scheduled_workflows[workflow_id]
        return {
            "scheduled": True,
            "next_run_time": schedule_info["next_run_time"].isoformat() if schedule_info["next_run_time"] else None,
            "trigger": schedule_info["trigger"],
        }
    else:
        return {"scheduled": False}


@router.get("/scheduled")
def list_scheduled_workflows(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List all scheduled workflows for the current user.
    """
    scheduled_workflows = workflow_scheduler.get_scheduled_workflows()

    # Filter to only workflows owned by current user
    user_scheduled = []
    for workflow_id, schedule_info in scheduled_workflows.items():
        workflow = crud.get_workflow(db, workflow_id)
        if workflow and workflow.owner_id == current_user.id:
            user_scheduled.append(
                {
                    "workflow_id": workflow_id,
                    "workflow_name": workflow.name,
                    "next_run_time": schedule_info["next_run_time"].isoformat()
                    if schedule_info["next_run_time"]
                    else None,
                    "trigger": schedule_info["trigger"],
                }
            )

    return {"scheduled_workflows": user_scheduled}
