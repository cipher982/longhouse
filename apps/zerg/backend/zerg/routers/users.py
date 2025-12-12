"""User profile routes.

Currently only *self-service* endpoints are exposed ("/users/me").  All
routes require authentication so we rely on the existing `get_current_user`
dependency to supply the active user.
"""

import json
from copy import deepcopy
from typing import Any, Dict

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import status
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db

# Auth guard ---------------------------------------------------------------
from zerg.dependencies.auth import get_current_user
from zerg.events import EventType
from zerg.events.decorators import publish_event
from zerg.schemas.schemas import UserOut
from zerg.schemas.schemas import UserUpdate
from zerg.schemas.user_context import UserContext

# Avatar helper
from zerg.services.avatar_service import store_avatar_for_user

router = APIRouter(tags=["users"], dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Deep Merge Helper
# ---------------------------------------------------------------------------


def deep_merge(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries, preserving nested keys.

    For nested dicts: recursively merge
    For other values: update replaces base
    Returns a new dict (does not modify inputs).

    Example:
        base = {"tools": {"location": true, "custom_tool": true}}
        update = {"tools": {"location": false}}
        result = {"tools": {"location": false, "custom_tool": true}}
    """
    result = deepcopy(base)
    for key, value in update.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            # Recursively merge nested dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Replace value
            result[key] = deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Request/Response Models for Context
# ---------------------------------------------------------------------------


class ContextResponse(BaseModel):
    """Response model for context endpoints."""

    context: Dict[str, Any]


class ContextUpdate(BaseModel):
    """Request model for updating context."""

    context: Dict[str, Any]


# ---------------------------------------------------------------------------
# /users/me – retrieve current profile
# ---------------------------------------------------------------------------


@router.get("/users/me", response_model=UserOut)
def read_current_user(current_user=Depends(get_current_user)):
    """Return the authenticated user's profile."""

    return current_user  # SQLAlchemy row – FastAPI will use attrs to dict


# ---------------------------------------------------------------------------
# /users/me – partial update
# ---------------------------------------------------------------------------


@router.put("/users/me", response_model=UserOut)
@publish_event(EventType.USER_UPDATED)
async def update_current_user(
    patch: UserUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Patch the authenticated user's profile (display name, avatar, prefs)."""

    updated = crud.update_user(
        db,
        current_user.id,
        display_name=patch.display_name,
        avatar_url=patch.avatar_url,
        prefs=patch.prefs,
    )

    if updated is None:
        # Should not happen if auth dependency returned a valid row.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return updated


# ---------------------------------------------------------------------------
# /users/me/avatar – upload user avatar
# ---------------------------------------------------------------------------


@router.post("/users/me/avatar", response_model=UserOut, status_code=status.HTTP_200_OK)
@publish_event(EventType.USER_UPDATED)
async def upload_current_user_avatar(
    *,
    file: UploadFile = File(..., description="Avatar image file (PNG/JPEG/WebP ≤2 MB)"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Handle *multipart/form-data* avatar upload for the authenticated user."""

    avatar_url = store_avatar_for_user(file)

    updated_user = crud.update_user(db, current_user.id, avatar_url=avatar_url)
    if updated_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return updated_user


# ---------------------------------------------------------------------------
# /users/me/context – context management
# ---------------------------------------------------------------------------


@router.get("/users/me/context", response_model=ContextResponse)
def get_user_context(current_user=Depends(get_current_user)):
    """Get the authenticated user's context configuration.

    Returns the user's context JSONB field which contains servers,
    integrations, preferences, and other user-specific data used
    for prompt composition.
    """
    return {"context": current_user.context or {}}


@router.patch("/users/me/context", response_model=ContextResponse)
@publish_event(EventType.USER_UPDATED)
async def update_user_context(
    update: ContextUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update (deep merge) the authenticated user's context.

    This endpoint deep-merges the provided context with the existing context.
    Nested objects (like 'tools') are merged recursively, preserving keys not
    present in the update. To replace the entire context, use PUT instead.

    Size limit: 64KB (65536 bytes) enforced on the merged result.

    The merged context is validated against the UserContext schema to catch
    common errors, but extra fields are allowed for flexibility.
    """
    # Deep merge with existing context (preserves nested keys)
    existing = current_user.context or {}
    merged = deep_merge(existing, update.context)

    # Validate merged result against schema (with extra fields allowed)
    try:
        UserContext.model_validate(merged)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Merged context validation failed: {str(e)}",
        )

    # Validate size limit on merged result (64KB)
    context_json = json.dumps(merged)
    if len(context_json.encode('utf-8')) > 65536:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Merged context too large (max 64KB)",
        )

    # Apply merged context
    current_user.context = merged

    db.commit()
    db.refresh(current_user)

    return {"context": current_user.context}


@router.put("/users/me/context", response_model=ContextResponse)
@publish_event(EventType.USER_UPDATED)
async def replace_user_context(
    update: ContextUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Replace the authenticated user's entire context.

    This endpoint replaces the entire context with the provided value.
    To merge with existing context, use PATCH instead.

    Size limit: 64KB (65536 bytes) enforced.

    The context is validated against the UserContext schema to catch common
    errors early, but extra fields are allowed for flexibility.
    """
    # Validate against schema (with extra fields allowed)
    try:
        UserContext.model_validate(update.context)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Context validation failed: {str(e)}",
        )

    # Validate size limit (64KB)
    context_json = json.dumps(update.context)
    if len(context_json.encode('utf-8')) > 65536:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Context too large (max 64KB)",
        )

    # Replace entire context
    current_user.context = update.context

    db.commit()
    db.refresh(current_user)

    return {"context": current_user.context}
