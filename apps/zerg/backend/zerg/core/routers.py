"""Clean API routers using dependency injection and business services.

These routers contain only HTTP-specific logic and delegate all business
operations to the appropriate services.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status

from zerg.core.factory import get_agent_service
from zerg.core.factory import get_auth_provider
from zerg.core.factory import get_thread_service
from zerg.core.interfaces import AuthProvider
from zerg.core.services import AgentService
from zerg.core.services import ThreadService
from zerg.models.models import User
from zerg.schemas.schemas import Agent
from zerg.schemas.schemas import AgentCreate
from zerg.schemas.schemas import AgentUpdate
from zerg.schemas.schemas import MessageCreate
from zerg.schemas.schemas import MessageResponse
from zerg.schemas.schemas import Thread


def get_current_user(
    request: Request,
    auth_provider: AuthProvider = Depends(get_auth_provider),
) -> User:
    """Get current authenticated user."""
    return auth_provider.get_current_user(request)


# Agent Router
agent_router = APIRouter()


@agent_router.get("/", response_model=List[Agent])
@agent_router.get("", response_model=List[Agent])
def list_agents(
    scope: str = Query("my", pattern="^(my|all)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> List[Agent]:
    """List agents for the current user."""
    try:
        return agent_service.list_agents(current_user, scope)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@agent_router.get("/{agent_id}", response_model=Agent)
def get_agent(
    agent_id: int,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> Agent:
    """Get agent by ID."""
    try:
        agent = agent_service.get_agent(agent_id, current_user)
        if not agent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        return agent
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@agent_router.post("/", response_model=Agent, status_code=status.HTTP_201_CREATED)
@agent_router.post("", response_model=Agent, status_code=status.HTTP_201_CREATED)
async def create_agent(
    agent: AgentCreate = Body(...),
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> Agent:
    """Create new agent."""
    try:
        return await agent_service.create_agent(
            user=current_user,
            name=agent.name,
            system_instructions=agent.system_instructions,
            task_instructions=agent.task_instructions,
            model=agent.model,
            schedule=agent.schedule,
            config=agent.config,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


@agent_router.put("/{agent_id}", response_model=Agent)
async def update_agent(
    agent_id: int,
    agent: AgentUpdate,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> Agent:
    """Update agent."""
    try:
        updated_agent = await agent_service.update_agent(
            agent_id=agent_id,
            user=current_user,
            name=agent.name,
            system_instructions=agent.system_instructions,
            task_instructions=agent.task_instructions,
            model=agent.model,
            status=agent.status.value if agent.status else None,
            schedule=agent.schedule,
            config=agent.config,
        )
        if not updated_agent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        return updated_agent
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@agent_router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: int,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
):
    """Delete agent."""
    try:
        success = await agent_service.delete_agent(agent_id, current_user)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@agent_router.get("/{agent_id}/messages", response_model=List[MessageResponse])
def get_agent_messages(
    agent_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> List[MessageResponse]:
    """Get messages for an agent."""
    try:
        return agent_service.get_agent_messages(agent_id, current_user, skip=skip, limit=limit)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@agent_router.post("/{agent_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def create_agent_message(
    agent_id: int,
    message: MessageCreate,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> MessageResponse:
    """Create message for an agent."""
    try:
        return agent_service.create_agent_message(agent_id, current_user, message.role, message.content)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


# Thread Router
thread_router = APIRouter()


@thread_router.get("/", response_model=List[Thread])
@thread_router.get("", response_model=List[Thread])
def list_threads(
    agent_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    thread_service: ThreadService = Depends(get_thread_service),
) -> List[Thread]:
    """List threads for the current user."""
    try:
        return thread_service.get_threads(current_user, agent_id=agent_id)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


# User Router
user_router = APIRouter()


@user_router.get("/me", response_model=Dict[str, Any])
def get_current_user_info(
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Get current user information."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "role": current_user.role,
        "display_name": current_user.display_name,
        "is_active": current_user.is_active,
    }
