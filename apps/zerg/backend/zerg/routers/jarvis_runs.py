"""Jarvis run history endpoints."""

import logging
from datetime import datetime
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.models.models import ThreadMessage
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


def _get_last_assistant_message(db: Session, thread_id: int) -> Optional[str]:
    """Get the last assistant message from a thread.

    Args:
        db: Database session
        thread_id: Thread ID to query

    Returns:
        Content of the last assistant message, or None if not found
    """
    import json

    last_msg = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .filter(ThreadMessage.role == "assistant")
        .order_by(ThreadMessage.id.desc())
        .first()
    )

    if not last_msg or not last_msg.content:
        return None

    content = last_msg.content

    # Handle string content (most common case)
    if isinstance(content, str):
        # Try to parse as JSON if it looks like structured content
        if content.startswith("[") or content.startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    # Handle array of content blocks
                    text_parts = []
                    for block in parsed:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    return " ".join(text_parts) if text_parts else content
            except (json.JSONDecodeError, TypeError):
                pass  # Not JSON, return as-is
        return content

    # Handle native list (if column supports JSON type)
    elif isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return " ".join(text_parts) if text_parts else None

    return str(content) if content else None


class RunStatusResponse(BaseModel):
    """Detailed status of a specific run."""

    run_id: int
    status: str
    created_at: datetime
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[str] = None


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run_status(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> RunStatusResponse:
    """Get current status of a specific run.

    Returns detailed status including timing, errors, and result if completed.
    This endpoint is used for polling run status after async task submission.

    Args:
        run_id: ID of the run to query
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        Run status with result if completed

    Raises:
        HTTPException: 404 if run not found or not owned by user
    """
    # Multi-tenant security: only return runs owned by the current user
    run = (
        db.query(AgentRun)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .filter(AgentRun.id == run_id)
        .filter(Agent.owner_id == current_user.id)
        .first()
    )

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Include result only if run succeeded
    result = None
    if run.status == RunStatus.SUCCESS:
        result = _get_last_assistant_message(db, run.thread_id)

    return RunStatusResponse(
        run_id=run.id,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        created_at=run.created_at,
        finished_at=run.finished_at,
        error=run.error,
        result=result,
    )


@router.get("/runs/{run_id}/stream")
async def attach_to_run_stream(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
):
    """Attach to an existing run's event stream.

    For MVP: Returns the current status and result if available.
    If run is complete, returns final result as JSON.
    If run is in progress, returns current status (streaming not implemented yet).

    Future enhancement: Stream remaining events for in-progress runs.
    Note: Infinite SSE streams in tests currently cause timeouts with httpx/ASGITransport.
    """
    # Multi-tenant security: only return runs owned by the current user
    run = (
        db.query(AgentRun)
        .join(Agent, Agent.id == AgentRun.agent_id)
        .filter(AgentRun.id == run_id)
        .filter(Agent.owner_id == current_user.id)
        .first()
    )

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Get result if available
    result = None
    if run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
        result = _get_last_assistant_message(db, run.thread_id)

    # Return status for both complete and in-progress runs
    return JSONResponse(
        {
            "run_id": run.id,
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "result": result,
            "error": run.error,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
    )


# TODO: Implement SSE re-attach for in-progress runs
# This would require:
# 1. Event log table to store all SSE events per run
# 2. Cursor-based replay from last_event_id
# 3. Hybrid: replay past events + stream new ones
# 4. Connection manager to handle multiple clients per run
