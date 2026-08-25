"""Browser-owned data deletion routes.

The Privacy and Security pages promise a user can delete a session, their whole
history, or their account. These are the endpoints behind that promise. They
delete immediately: no soft delete, no grace period, no recovery.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import Field

from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.services.data_deletion import DataDeletionUnavailable
from zerg.services.data_deletion import SessionNotFound
from zerg.services.data_deletion import delete_account_data
from zerg.services.data_deletion import delete_session_data
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/user-data",
    tags=["user-data"],
    dependencies=[Depends(get_current_browser_user), Depends(require_single_tenant)],
)


class SessionDeletionResponse(UTCBaseModel):
    session_id: str
    already_deleted: bool
    raw_objects_deleted: int
    render_objects_deleted: int
    object_bytes_deleted: int
    manifest_rows_retired: int
    live_rows_removed: int
    search_index_removed: bool
    partial: list[str]


class AccountDeletionRequest(UTCBaseModel):
    confirm: bool = Field(..., description="Must be true. Account data deletion is immediate and irreversible.")


class AccountDeletionResponse(UTCBaseModel):
    owner_id: int
    sessions_deleted: int
    raw_objects_deleted: int
    render_objects_deleted: int
    object_bytes_deleted: int
    manifest_rows_retired: int
    live_rows_removed: int
    search_indexes_removed: int
    partial: list[str]


@router.delete("/sessions/{session_id}", response_model=SessionDeletionResponse)
async def delete_user_session(
    session_id: UUID,
    current_user=Depends(get_current_browser_user),
) -> SessionDeletionResponse:
    """Delete one of the caller's sessions from every store that can be reached."""
    try:
        report = await delete_session_data(session_id=session_id, owner_id=int(current_user.id))
    except SessionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found") from exc
    except DataDeletionUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return SessionDeletionResponse(**asdict(report))


@router.delete("/account", response_model=AccountDeletionResponse)
async def delete_user_account_data(
    body: AccountDeletionRequest,
    current_user=Depends(get_current_browser_user),
) -> AccountDeletionResponse:
    """Delete every session of the caller's that can be reached owner-scoped."""
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirm must be true to delete your account data",
        )
    try:
        report = await delete_account_data(owner_id=int(current_user.id))
    except DataDeletionUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return AccountDeletionResponse(**asdict(report))
