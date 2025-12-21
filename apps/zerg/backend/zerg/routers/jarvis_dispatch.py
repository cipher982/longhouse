"""Jarvis manual agent dispatch endpoint."""

import logging
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.models.models import AgentRun
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.services.task_runner import execute_agent_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


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
        run = db.query(AgentRun).filter(AgentRun.thread_id == thread.id).order_by(AgentRun.created_at.desc()).first()

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
