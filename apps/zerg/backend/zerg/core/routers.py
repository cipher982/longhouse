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

from zerg.core.factory import get_auth_provider
from zerg.core.factory import get_fiche_service
from zerg.core.factory import get_thread_service
from zerg.core.interfaces import AuthProvider
from zerg.core.services import FicheService
from zerg.core.services import ThreadService
from zerg.models.models import User
from zerg.schemas.schemas import Fiche
from zerg.schemas.schemas import FicheCreate
from zerg.schemas.schemas import FicheUpdate
from zerg.schemas.schemas import MessageCreate
from zerg.schemas.schemas import MessageResponse
from zerg.schemas.schemas import Thread


def get_current_user(
    request: Request,
    auth_provider: AuthProvider = Depends(get_auth_provider),
) -> User:
    """Get current authenticated user."""
    return auth_provider.get_current_user(request)


# Fiche Router
fiche_router = APIRouter()


@fiche_router.get("/", response_model=List[Fiche])
@fiche_router.get("", response_model=List[Fiche])
def list_fiches(
    scope: str = Query("my", pattern="^(my|all)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    fiche_service: FicheService = Depends(get_fiche_service),
) -> List[Fiche]:
    """List fiches for the current user."""
    try:
        return fiche_service.list_fiches(current_user, scope)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@fiche_router.get("/{fiche_id}", response_model=Fiche)
def get_fiche(
    fiche_id: int,
    current_user: User = Depends(get_current_user),
    fiche_service: FicheService = Depends(get_fiche_service),
) -> Fiche:
    """Get fiche by ID."""
    try:
        fiche = fiche_service.get_fiche(fiche_id, current_user)
        if not fiche:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
        return fiche
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@fiche_router.post("/", response_model=Fiche, status_code=status.HTTP_201_CREATED)
@fiche_router.post("", response_model=Fiche, status_code=status.HTTP_201_CREATED)
async def create_fiche(
    fiche: FicheCreate = Body(...),
    current_user: User = Depends(get_current_user),
    fiche_service: FicheService = Depends(get_fiche_service),
) -> Fiche:
    """Create new fiche."""
    try:
        return await fiche_service.create_fiche(
            user=current_user,
            name=fiche.name,
            system_instructions=fiche.system_instructions,
            task_instructions=fiche.task_instructions,
            model=fiche.model,
            schedule=fiche.schedule,
            config=fiche.config,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


@fiche_router.put("/{fiche_id}", response_model=Fiche)
async def update_fiche(
    fiche_id: int,
    fiche: FicheUpdate,
    current_user: User = Depends(get_current_user),
    fiche_service: FicheService = Depends(get_fiche_service),
) -> Fiche:
    """Update fiche."""
    try:
        updated_fiche = await fiche_service.update_fiche(
            fiche_id=fiche_id,
            user=current_user,
            name=fiche.name,
            system_instructions=fiche.system_instructions,
            task_instructions=fiche.task_instructions,
            model=fiche.model,
            status=fiche.status.value if fiche.status else None,
            schedule=fiche.schedule,
            config=fiche.config,
        )
        if not updated_fiche:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
        return updated_fiche
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@fiche_router.delete("/{fiche_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fiche(
    fiche_id: int,
    current_user: User = Depends(get_current_user),
    fiche_service: FicheService = Depends(get_fiche_service),
):
    """Delete fiche."""
    try:
        success = await fiche_service.delete_fiche(fiche_id, current_user)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@fiche_router.get("/{fiche_id}/messages", response_model=List[MessageResponse])
def get_fiche_messages(
    fiche_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    fiche_service: FicheService = Depends(get_fiche_service),
) -> List[MessageResponse]:
    """Get messages for a fiche."""
    try:
        return fiche_service.get_fiche_messages(fiche_id, current_user, skip=skip, limit=limit)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@fiche_router.post("/{fiche_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def create_fiche_message(
    fiche_id: int,
    message: MessageCreate,
    current_user: User = Depends(get_current_user),
    fiche_service: FicheService = Depends(get_fiche_service),
) -> MessageResponse:
    """Create message for a fiche."""
    try:
        return fiche_service.create_fiche_message(fiche_id, current_user, message.role, message.content)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


# Thread Router
thread_router = APIRouter()


@thread_router.get("/", response_model=List[Thread])
@thread_router.get("", response_model=List[Thread])
def list_threads(
    fiche_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    thread_service: ThreadService = Depends(get_thread_service),
) -> List[Thread]:
    """List threads for the current user."""
    try:
        return thread_service.get_threads(current_user, fiche_id=fiche_id)
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
