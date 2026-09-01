"""Browser and public endpoints for explicit session share links -- DISABLED.

Nothing in this module is served. ``router`` and ``public_router`` -- the two
names ``main.py`` mounts -- are empty on purpose; every handler below is
decorated onto ``_shelved``/``_shelved_public``, which nothing includes.

The blocker is the schema, not the code. ``SessionShare`` and
``SessionShareEvent`` (``zerg/models/session_share.py``) are declared on the
archive ``Base``. Every real deployment runs the live catalog, and catalogd
creates its own set of schemas -- the share tables are in none of them, so a
share has nowhere to be written. ``server/tests_lite/test_session_shares.py``
passes because it calls ``Base.metadata.create_all()`` against a schema
production never has; that is why this shipped looking green.

The design is worth reviving: HMAC tokens hashed at rest, TTL-bounded,
revocable, with the only real audit table in the codebase. To bring it back,
give the two tables a home catalogd actually creates, point ``router`` and
``public_router`` at the shelved routers, and run ``make generate-sdk``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import catalog_db_dependency
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_caller
from zerg.services.session_shares import DEFAULT_SHARE_TTL_DAYS
from zerg.services.session_shares import SessionShareError
from zerg.services.session_shares import SessionShareNotFound
from zerg.services.session_shares import create_session_share
from zerg.services.session_shares import resolve_session_share
from zerg.services.session_shares import revoke_session_share
from zerg.services.session_views import SessionSharerResponse
from zerg.utils.time import UTCBaseModel

# The two routers ``main.py`` includes. Empty is how "not registered" is
# expressed from inside this file, and it is the stronger of the two disables:
# mounting the handlers to return 404/501 would keep four dead endpoints in the
# published OpenAPI schema -- still advertising a capability that cannot
# exist -- and would leave the unauthenticated
# ``/public/session-shares/{token}/preview`` route reachable. With nothing
# mounted there is no route to match, so no handler, no dependency chain, and
# no contract for clients to generate against.
router = APIRouter()
public_router = APIRouter()

# Shelved, not deleted. Everything below hangs off these; nothing includes them.
_shelved = APIRouter(tags=["session-shares"], dependencies=[Depends(require_single_tenant)])
_shelved_public = APIRouter(
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


def _share_store_db():
    """Yield no share store at all.

    ``session_shares`` and ``session_share_events`` are declared on the archive
    ``Base``; they exist in no other schema, and catalogd creates none of them.
    ``get_db`` used to raise 503 from the dependency, before any handler body
    ran -- which is exactly what kept the ownership check below off the
    production path. Yielding None instead puts the check first and lets the
    handler name what is actually unavailable.
    """

    yield None


# Same seam as the neighbouring routers: keep ``get_db`` as the exact callable
# in legacy/test mode so dependency overrides still bind, and take the
# catalog-aware generator everywhere a live catalog is configured.
_share_store_db_dependency = get_db if catalog_db_dependency() is get_db else _share_store_db


def _require_share_store(db: Session | None) -> Session:
    """Fail loudly when share records have no store on this deployment."""

    if db is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "session_shares_unavailable",
                "message": (
                    "Share links are unavailable: share records live only in the archive schema, which this instance does not serve."
                ),
            },
        )
    return db


def _require_session_owner(*, session_id: UUID, user_id: int) -> None:
    """Fail closed unless the caller demonstrably owns this session.

    Ownership is resolved where the canonical session facts live: catalogd's
    owner-scoped session read, which returns nothing unless a durable row binds
    the session to this owner. An unreachable catalogd also returns nothing, so
    the closed direction is the failure direction.
    """
    from zerg.services.live_control_catalog import load_live_control_session_snapshot

    if load_live_control_session_snapshot(session_id, owner_id=user_id) is None:
        _raise_share_error(SessionShareNotFound())


@_shelved.post("/timeline/sessions/{session_id}/shares", response_model=SessionShareResponse)
def create_timeline_session_share(
    session_id: UUID,
    body: CreateSessionShareRequest | None = None,
    db: Session | None = Depends(_share_store_db_dependency),
    current_user=Depends(get_current_browser_caller),
) -> SessionShareResponse:
    body = body or CreateSessionShareRequest()
    # Ownership first, on both backends, so no deployment can mint a link for a
    # session the caller does not own -- and so an unavailable store cannot
    # short-circuit the check.
    _require_session_owner(session_id=session_id, user_id=int(current_user.id))
    db = _require_share_store(db)
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


@_shelved.delete("/timeline/session-shares/{share_id}", response_model=SessionShareResolveResponse)
def revoke_timeline_session_share(
    share_id: int,
    db: Session | None = Depends(_share_store_db_dependency),
    current_user=Depends(get_current_browser_caller),
) -> SessionShareResolveResponse:
    db = _require_share_store(db)
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


@_shelved.get("/timeline/session-shares/{token}/resolve", response_model=SessionShareResolveResponse)
def resolve_timeline_session_share(
    token: str,
    db: Session | None = Depends(_share_store_db_dependency),
    current_user=Depends(get_current_browser_caller),
) -> SessionShareResolveResponse:
    db = _require_share_store(db)
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


@_shelved_public.get("/{token}/preview", response_model=SessionSharePreviewResponse)
def preview_public_session_share(
    token: str,
    db: Session | None = Depends(_share_store_db_dependency),
) -> SessionSharePreviewResponse:
    db = _require_share_store(db)
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
