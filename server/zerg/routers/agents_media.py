"""Agents API for archive media claims, upload, and blob fetch."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.auth.caller import Caller
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.browser_route_auth import get_current_browser_route_caller
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef
from zerg.services.catalog_read_gateway import session_batch_snapshot
from zerg.services.media_store import MAX_MEDIA_BYTES
from zerg.services.media_store import absolute_media_path
from zerg.services.media_store import claim_media
from zerg.services.media_store import is_valid_sha256
from zerg.services.media_store import store_media_blob

router = APIRouter(prefix="/agents/media", tags=["agents"])
browser_router = APIRouter(prefix="/media", tags=["media"])


class MediaClaimItem(BaseModel):
    sha256: str
    mime_type: str | None = None
    byte_size: int | None = None
    session_id: UUID | None = None
    event_id: int | None = None
    source_path: str | None = None
    source_offset: int | None = None
    source_line_hash: str | None = None
    json_pointer: str | None = None
    provider: str | None = None
    original_kind: str | None = None


class MediaClaimsRequest(BaseModel):
    items: list[MediaClaimItem]


class MediaClaimsResponse(BaseModel):
    needed: list[str]
    present: list[str]
    rejected: list[dict[str, str]]


class MediaUploadResponse(BaseModel):
    sha256: str
    mime_type: str
    byte_size: int
    created: bool
    blob_url: str


def _content_type(request: Request) -> str:
    return (request.headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()


async def _read_bounded_body(request: Request) -> bytes:
    """Read an upload body without ever buffering more than one media object.

    `await request.body()` buffers whatever the client sends before
    store_media_blob() gets to check the size, so the ceiling has to be applied
    while the stream is still arriving.
    """

    content_encoding = (request.headers.get("content-encoding") or "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="media upload accepts identity encoding only",
        )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid content-length") from exc
        if size > MAX_MEDIA_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"media exceeds {MAX_MEDIA_BYTES} bytes",
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_MEDIA_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"media exceeds {MAX_MEDIA_BYTES} bytes",
            )
        body.extend(chunk)
    return bytes(body)


def _row_or_404(db: Session, sha256: str) -> MediaObject:
    if not is_valid_sha256(sha256):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")
    row = db.query(MediaObject).filter(MediaObject.sha256 == sha256).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")
    path = absolute_media_path(row)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media blob missing")
    return row


def _owner_id(identity: object, *, field: str) -> int | None:
    raw_owner_id = getattr(identity, field, None)
    if raw_owner_id is None:
        return None
    try:
        return int(raw_owner_id)
    except (TypeError, ValueError):
        return None


async def _owner_row_or_404(db: Session, sha256: str, owner_id: int | None) -> MediaObject:
    row = _row_or_404(db, sha256)
    if owner_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")
    session_ids = [
        str(value)
        for (value,) in (db.query(SessionMediaRef.session_id).filter(SessionMediaRef.media_sha256 == row.sha256).distinct().all())
    ]
    for offset in range(0, len(session_ids), 20):
        snapshot = await asyncio.to_thread(
            session_batch_snapshot,
            session_ids[offset : offset + 20],
            owner_id=owner_id,
        )
        if snapshot.get("facts"):
            return row
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")


def _stream_media_row(row: MediaObject) -> StreamingResponse:
    path = absolute_media_path(row)
    return StreamingResponse(
        path.open("rb"),
        media_type=row.mime_type,
        headers={
            "Content-Length": str(row.byte_size),
            "X-Media-Sha256": row.sha256,
        },
    )


def _head_media_row(row: MediaObject) -> Response:
    return Response(
        status_code=status.HTTP_200_OK,
        media_type=row.mime_type,
        headers={
            "Content-Length": str(row.byte_size),
            "X-Media-Sha256": row.sha256,
        },
    )


@router.post(
    "/claims",
    response_model=MediaClaimsResponse,
    dependencies=[Depends(verify_agents_caller), Depends(require_single_tenant)],
)
async def create_media_claims(request: MediaClaimsRequest, db: Session = Depends(get_db)) -> MediaClaimsResponse:
    """Return which content-addressed media blobs this Runtime Host needs."""

    if len(request.items) > 512:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="too many media claim items")
    result = claim_media(db, [item.model_dump() for item in request.items])
    return MediaClaimsResponse(needed=result.needed, present=result.present, rejected=result.rejected)


@router.put(
    "/{sha256}",
    response_model=MediaUploadResponse,
    dependencies=[Depends(verify_agents_caller), Depends(require_single_tenant)],
)
async def put_media_blob(
    sha256: str,
    request: Request,
    db: Session = Depends(get_db),
    first_seen_session_id: UUID | None = Header(default=None, alias="X-Longhouse-Session-Id"),
) -> MediaUploadResponse:
    """Upload a media blob once, keyed by sha256."""

    data = await _read_bounded_body(request)
    try:
        stored = store_media_blob(
            db,
            sha256=sha256,
            mime_type=_content_type(request),
            data=data,
            first_seen_session_id=first_seen_session_id,
        )
    except ValueError as exc:
        status_code = status.HTTP_409_CONFLICT if str(exc) == "sha256 mismatch" else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    return MediaUploadResponse(
        sha256=stored.sha256,
        mime_type=stored.mime_type,
        byte_size=stored.byte_size,
        created=stored.created,
        blob_url=f"/api/agents/media/{stored.sha256}/blob",
    )


@router.get(
    "/{sha256}/blob",
    dependencies=[Depends(require_single_tenant)],
)
async def get_media_blob(
    sha256: str,
    db: Session = Depends(get_db),
    auth: object = Depends(verify_agents_caller),
) -> StreamingResponse:
    """Fetch a media blob by sha256 over machine-token auth."""

    row = await _owner_row_or_404(db, sha256, _owner_id(auth, field="owner_id"))
    return _stream_media_row(row)


@router.head(
    "/{sha256}",
    dependencies=[Depends(require_single_tenant)],
)
async def head_media_blob(
    sha256: str,
    db: Session = Depends(get_db),
    auth: object = Depends(verify_agents_caller),
) -> Response:
    """Cheap integrity probe for a media blob."""

    row = await _owner_row_or_404(db, sha256, _owner_id(auth, field="owner_id"))
    return _head_media_row(row)


@browser_router.get("/{sha256}/blob")
async def get_browser_media_blob(
    sha256: str,
    db: Session = Depends(get_db),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> StreamingResponse:
    """Fetch a browser-visible media blob by sha256."""

    row = await _owner_row_or_404(db, sha256, _owner_id(current_user, field="id"))
    return _stream_media_row(row)


@browser_router.get("/{sha256}/thumb")
async def get_browser_media_thumbnail(
    sha256: str,
    db: Session = Depends(get_db),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> StreamingResponse:
    """Fetch a derived thumbnail for a browser-visible media object."""

    row = await _owner_row_or_404(db, sha256, _owner_id(current_user, field="id"))
    if not row.thumbnail_sha256:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media thumbnail not found")
    thumb_row = _row_or_404(db, row.thumbnail_sha256)
    return _stream_media_row(thumb_row)


@browser_router.head("/{sha256}")
async def head_browser_media_blob(
    sha256: str,
    db: Session = Depends(get_db),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> Response:
    """Cheap browser integrity probe for a visible media blob."""

    row = await _owner_row_or_404(db, sha256, _owner_id(current_user, field="id"))
    return _head_media_row(row)
