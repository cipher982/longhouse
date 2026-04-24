"""User profile routes.

Currently only *self-service* endpoints are exposed ("/users/me").  All
routes require authentication so we rely on the existing `get_current_user`
dependency to supply the active user.
"""

from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.crud import update_user
from zerg.database import get_db

# Auth guard ---------------------------------------------------------------
from zerg.dependencies.auth import get_current_user
from zerg.events import EventType
from zerg.events.decorators import publish_event
from zerg.models.user import User
from zerg.schemas.schemas import UserOut
from zerg.schemas.schemas import UserUpdate
from zerg.schemas.usage import UserUsageResponse
from zerg.services.apns_sender import set_user_apns_enabled
from zerg.services.apns_sender import user_apns_enabled

# Avatar helper
from zerg.services.avatar_service import store_avatar_for_user

# Usage service
from zerg.services.usage_service import get_user_usage

router = APIRouter(tags=["users"], dependencies=[Depends(get_current_user)])


class UserNotificationSettingsResponse(BaseModel):
    apns_enabled: bool


class UserNotificationSettingsUpdate(BaseModel):
    apns_enabled: bool


# ---------------------------------------------------------------------------
# /users/me – retrieve current profile
# ---------------------------------------------------------------------------


@router.get("/users/me", response_model=UserOut)
def read_current_user(current_user=Depends(get_current_user)):
    """Return the authenticated user's profile."""

    return current_user  # SQLAlchemy row – FastAPI will use attrs to dict


# ---------------------------------------------------------------------------
# /users/me/usage – LLM usage stats
# ---------------------------------------------------------------------------


@router.get("/users/me/usage", response_model=UserUsageResponse)
def read_current_user_usage(
    period: Literal["today", "7d", "30d"] = Query("today", description="Time period for usage stats"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return the authenticated user's LLM usage stats.

    Returns token counts, costs, and daily budget limit status.
    The `limit` field always reflects today's daily limit usage,
    regardless of the selected period.
    """
    return get_user_usage(db, current_user.id, period)


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

    updated = update_user(
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


@router.get("/users/me/notifications", response_model=UserNotificationSettingsResponse)
def read_current_user_notification_settings(current_user=Depends(get_current_user)) -> UserNotificationSettingsResponse:
    """Return the authenticated user's mobile notification settings."""

    return UserNotificationSettingsResponse(apns_enabled=user_apns_enabled(current_user))


@router.patch("/users/me/notifications", response_model=UserNotificationSettingsResponse)
@publish_event(EventType.USER_UPDATED)
async def update_current_user_notification_settings(
    patch: UserNotificationSettingsUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> UserNotificationSettingsResponse:
    """Update mobile notification preferences for the authenticated user."""

    user = db.query(User).filter(User.id == current_user.id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    set_user_apns_enabled(user, patch.apns_enabled)
    db.commit()
    db.refresh(user)
    return UserNotificationSettingsResponse(apns_enabled=user_apns_enabled(user))


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

    updated_user = update_user(db, current_user.id, avatar_url=avatar_url)
    if updated_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return updated_user
