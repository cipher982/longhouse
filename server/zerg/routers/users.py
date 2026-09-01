"""User profile routes.

Currently only *self-service* endpoints are exposed ("/users/me").  All
routes require authentication so we rely on the existing `get_current_user`
dependency to supply the active user.
"""

import asyncio
from datetime import datetime
from datetime import timezone
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.auth.catalog_gateway import update_user

# Auth guard ---------------------------------------------------------------
from zerg.dependencies.auth import get_current_user
from zerg.dependencies.form_post_origin import reject_cross_origin_form_post
from zerg.dependencies.request_db import no_request_db
from zerg.events import EventType
from zerg.events.decorators import publish_event
from zerg.schemas.schemas import UserOut
from zerg.schemas.schemas import UserUpdate

# Avatar helper
from zerg.services.avatar_service import store_avatar_for_user
from zerg.services.notification_policy import apply_user_notification_prefs
from zerg.services.notification_policy import load_user_notification_prefs
from zerg.utils.time import UTCBaseModel

router = APIRouter(tags=["users"], dependencies=[Depends(get_current_user)])


class UserNotificationSettingsResponse(BaseModel):
    apns_enabled: bool
    notify_only_when_away: bool = False
    time_sensitive_blocked: bool = False
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class UserNotificationSettingsUpdate(BaseModel):
    apns_enabled: bool | None = None
    notify_only_when_away: bool | None = None
    time_sensitive_blocked: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class UserClientPresenceHeartbeat(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)
    client_type: Literal["web"] = "web"
    visible: bool
    route: str | None = Field(default=None, max_length=512)
    session_id: str | None = Field(default=None, max_length=80)


class UserClientPresenceResponse(UTCBaseModel):
    client_id: str
    client_type: Literal["web"]
    visible: bool
    route: str | None
    session_id: str | None
    last_seen_at: datetime


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
    current_user=Depends(get_current_user),
):
    """Patch the authenticated user's profile (display name, avatar, prefs)."""

    update_mask = [
        field for field in ("display_name", "avatar_url", "prefs") if field in patch.model_fields_set and getattr(patch, field) is not None
    ]
    result = await asyncio.to_thread(
        update_user,
        user_id=current_user.id,
        display_name=patch.display_name,
        avatar_url=patch.avatar_url,
        prefs=patch.prefs,
        update_mask=update_mask,
    )
    if result.get("found") is not True:
        # Should not happen if auth dependency returned a valid row.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return result["user"]


@router.get("/users/me/notifications", response_model=UserNotificationSettingsResponse)
def read_current_user_notification_settings(current_user=Depends(get_current_user)) -> UserNotificationSettingsResponse:
    """Return the authenticated user's mobile notification settings."""

    prefs = load_user_notification_prefs(current_user)
    return UserNotificationSettingsResponse(
        apns_enabled=prefs.apns_enabled,
        notify_only_when_away=prefs.notify_only_when_away,
        time_sensitive_blocked=prefs.time_sensitive_blocked,
        quiet_hours_start=prefs.quiet_hours_start,
        quiet_hours_end=prefs.quiet_hours_end,
    )


@router.patch("/users/me/notifications", response_model=UserNotificationSettingsResponse)
@publish_event(EventType.USER_UPDATED)
async def update_current_user_notification_settings(
    patch: UserNotificationSettingsUpdate,
    current_user=Depends(get_current_user),
) -> UserNotificationSettingsResponse:
    """Update mobile notification preferences for the authenticated user."""

    updates = patch.model_dump(exclude_unset=True)
    if not updates:
        prefs = load_user_notification_prefs(current_user)
    else:
        try:
            prefs = apply_user_notification_prefs(current_user, updates)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        result = await asyncio.to_thread(
            update_user,
            user_id=int(current_user.id),
            prefs=dict(current_user.prefs or {}),
            update_mask=["prefs"],
        )
        if result.get("found") is not True:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserNotificationSettingsResponse(
        apns_enabled=prefs.apns_enabled,
        notify_only_when_away=prefs.notify_only_when_away,
        time_sensitive_blocked=prefs.time_sensitive_blocked,
        quiet_hours_start=prefs.quiet_hours_start,
        quiet_hours_end=prefs.quiet_hours_end,
    )


@router.post("/users/me/client-presence", response_model=UserClientPresenceResponse)
async def update_current_user_client_presence(
    heartbeat: UserClientPresenceHeartbeat,
    db: Session | None = Depends(no_request_db),
    current_user=Depends(get_current_user),
) -> UserClientPresenceResponse:
    """Record whether a browser client is actively watching Longhouse."""

    owner_id = int(current_user.id)
    now = datetime.now(timezone.utc)

    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=503, detail="Live notification presence catalog is unavailable")
    result = await catalogd.call(
        "notification.presence.upsert.v2",
        {
            "owner_id": owner_id,
            "client_id": heartbeat.client_id,
            "client_type": heartbeat.client_type,
            "visible": heartbeat.visible,
            "route": heartbeat.route,
            "session_id": heartbeat.session_id,
            "observed_at": now.isoformat(),
        },
        timeout_seconds=1.0,
    )
    return UserClientPresenceResponse(**result["presence"])


# ---------------------------------------------------------------------------
# /users/me/avatar – upload user avatar
# ---------------------------------------------------------------------------


@router.post("/users/me/avatar", response_model=UserOut, status_code=status.HTTP_200_OK)
@publish_event(EventType.USER_UPDATED)
async def upload_current_user_avatar(
    request: Request,
    *,
    file: UploadFile = File(..., description="Avatar image file (PNG/JPEG/WebP ≤2 MB)"),
    current_user=Depends(get_current_user),
):
    """Handle *multipart/form-data* avatar upload for the authenticated user."""

    reject_cross_origin_form_post(request)

    avatar_url = store_avatar_for_user(file)

    result = await asyncio.to_thread(
        update_user,
        user_id=int(current_user.id),
        avatar_url=avatar_url,
        update_mask=["avatar_url"],
    )
    if result.get("found") is not True:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return result["user"]
