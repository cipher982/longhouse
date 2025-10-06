"""User profile routes.

Currently only *self-service* endpoints are exposed ("/users/me").  All
routes require authentication so we rely on the existing `get_current_user`
dependency to supply the active user.
"""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import status
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db

# Auth guard ---------------------------------------------------------------
from zerg.dependencies.auth import get_current_user
from zerg.events import EventType
from zerg.events.decorators import publish_event
from zerg.schemas.schemas import UserOut
from zerg.schemas.schemas import UserUpdate

# Avatar helper
from zerg.services.avatar_service import store_avatar_for_user

router = APIRouter(tags=["users"], dependencies=[Depends(get_current_user)])


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
