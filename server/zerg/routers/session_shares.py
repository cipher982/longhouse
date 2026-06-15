"""Browser and public endpoints for explicit session share links."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.services.session_shares import DEFAULT_SHARE_TTL_DAYS
from zerg.services.session_shares import SessionShareError
from zerg.services.session_shares import create_session_share
from zerg.services.session_shares import resolve_session_share
from zerg.services.session_shares import revoke_session_share
from zerg.services.session_views import SessionSharerResponse
from zerg.utils.time import UTCBaseModel

router = APIRouter(tags=["session-shares"], dependencies=[Depends(require_single_tenant)])
public_router = APIRouter(
    prefix="/public/session-shares",
    tags=["session-shares"],
    dependencies=[Depends(require_single_tenant)],
)


class CreateSessionShareRequest(UTCBaseModel):
    expires_in_days: Optional[int] = Field(
        DEFAULT_SHARE_TTL_DAYS,
        ge=1,
        le=365,
        description="Optional TTL for the share link. Defaults to 30 days.",
    )
    note: Optional[str] = Field(None, max_length=280, description="Optional short note shown on the share landing page.")


class SessionShareResponse(UTCBaseModel):
    id: int
    session_id: str
    token: str
    share_url: str
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]
    sharer: Optional[SessionSharerResponse]


class SessionSharePreviewResponse(UTCBaseModel):
    provider: str
    device_name: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    expires_at: Optional[datetime]
    note: Optional[str]
    sharer: Optional[SessionSharerResponse]


class SessionShareResolveResponse(UTCBaseModel):
    session_id: str
    share_id: int
    expires_at: Optional[datetime]
    note: Optional[str]
    sharer: Optional[SessionSharerResponse]


def _raise_share_error(exc: SessionShareError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/timeline/sessions/{session_id}/shares", response_model=SessionShareResponse)
def create_timeline_session_share(
    session_id: UUID,
    body: CreateSessionShareRequest | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
) -> SessionShareResponse:
    body = body or CreateSessionShareRequest()
    try:
        share, token = create_session_share(
            db,
            session_id=session_id,
            created_by_user_id=int(current_user.id),
            expires_in_days=body.expires_in_days,
            note=body.note,
        )
        resolved = resolve_session_share(db, token=token, expected_session_id=session_id)
    except SessionShareError as exc:
        _raise_share_error(exc)

    return SessionShareResponse(
        id=int(share.id),
        session_id=str(share.session_id),
        token=token,
        share_url=f"/share/{token}",
        expires_at=share.expires_at,
        revoked_at=share.revoked_at,
        sharer=resolved.sharer,
    )


@router.delete("/timeline/session-shares/{share_id}", response_model=SessionShareResolveResponse)
def revoke_timeline_session_share(
    share_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
) -> SessionShareResolveResponse:
    try:
        share = revoke_session_share(db, share_id=share_id, actor_user_id=int(current_user.id))
    except SessionShareError as exc:
        _raise_share_error(exc)
    return SessionShareResolveResponse(
        session_id=str(share.session_id),
        share_id=int(share.id),
        expires_at=share.expires_at,
        note=share.note,
        sharer=None,
    )


@router.get("/timeline/session-shares/{token}/resolve", response_model=SessionShareResolveResponse)
def resolve_timeline_session_share(
    token: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
) -> SessionShareResolveResponse:
    try:
        resolved = resolve_session_share(db, token=token, actor_user_id=int(current_user.id), record_access=True)
    except SessionShareError as exc:
        _raise_share_error(exc)
    return SessionShareResolveResponse(
        session_id=str(resolved.session.id),
        share_id=int(resolved.share.id),
        expires_at=resolved.share.expires_at,
        note=resolved.share.note,
        sharer=resolved.sharer,
    )


@public_router.get("/{token}/preview", response_model=SessionSharePreviewResponse)
def preview_public_session_share(
    token: str,
    db: Session = Depends(get_db),
) -> SessionSharePreviewResponse:
    try:
        resolved = resolve_session_share(db, token=token)
    except SessionShareError as exc:
        _raise_share_error(exc)
    session = resolved.session
    return SessionSharePreviewResponse(
        provider=session.provider,
        device_name=session.device_name,
        started_at=session.started_at,
        ended_at=session.ended_at,
        expires_at=resolved.share.expires_at,
        note=resolved.share.note,
        sharer=resolved.sharer,
    )
