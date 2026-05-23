"""Multipart input + attachment blob fetch endpoints.

The browser/iOS composer hits ``POST /api/sessions/{id}/inputs-multipart``
when the user attaches one or more images. This is parallel to the JSON
``POST /sessions/{id}/input`` path so the no-attachment flow stays
untouched and zero-overhead.

The Machine Agent fetches the blob bytes through
``GET /api/agents/sessions/{sid}/inputs/{iid}/attachments/{aid}/blob``
using the standard ``X-Agents-Token`` header and a per-session 404 boundary
so a leaked attachment id can never read across sessions.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import Header
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.metrics import session_input_attachment_blob_fetches_total
from zerg.metrics import session_input_attachment_bytes
from zerg.metrics import session_input_attachments_total
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.routers.session_chat import QueuedInputSummary
from zerg.routers.session_chat import SessionInputResponse
from zerg.services.session_chat_impl import _assert_live_session_send_available
from zerg.services.session_chat_impl import _build_managed_local_chat_response
from zerg.services.session_chat_impl import _load_session_for_continuation
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_current_control import current_session_capabilities
from zerg.services.session_input_attachments import ALLOWED_MIME_TYPES
from zerg.services.session_input_attachments import MAX_ATTACHMENT_BYTES
from zerg.services.session_input_attachments import MAX_ATTACHMENTS_PER_INPUT
from zerg.services.session_input_attachments import StoredAttachment
from zerg.services.session_input_attachments import absolute_blob_path
from zerg.services.session_input_attachments import get_attachment
from zerg.services.session_input_attachments import list_attachments_for_input
from zerg.services.session_input_attachments import store_attachment_blob
from zerg.services.session_inputs import INPUT_INTENT_AUTO
from zerg.services.session_inputs import INPUT_INTENT_STEER
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import create_session_input
from zerg.services.session_inputs import mark_delivered
from zerg.services.session_inputs import mark_failed

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])


def _attachment_ref_for_engine(
    *,
    session_id: str,
    input_id: int,
    stored: StoredAttachment,
) -> dict:
    """Build the JSON the engine needs to fetch this blob over machine auth.

    The path is relative to the runtime host's public origin; the engine
    resolves it against its own ``api_url`` so we don't need to know the
    public hostname here. Sha256 + mime + id round-trip into the engine's
    ``AttachmentRef``.
    """
    return {
        "id": str(stored.id),
        "mime_type": stored.mime_type,
        "sha256": stored.sha256,
        "blob_url": (
            f"/api/agents/sessions/{session_id}"
            f"/inputs/{input_id}/attachments/{stored.id}/blob"
        ),
    }


def _validate_attachments(files: List[UploadFile]) -> None:
    if len(files) > MAX_ATTACHMENTS_PER_INPUT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"too many attachments (max {MAX_ATTACHMENTS_PER_INPUT})",
        )
    for upload in files:
        if upload.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported attachment type: {upload.content_type}",
            )


def _queued_summary_from_row(row) -> QueuedInputSummary:
    return QueuedInputSummary(
        id=int(row.id),
        text=row.body,
        intent=row.intent,
        status=row.status,
        last_error=row.last_error,
        created_at=row.created_at,
    )


def _client_label_from_user_agent(user_agent: str | None) -> str:
    if not user_agent:
        return "unknown"
    lowered = user_agent.lower()
    if "longhouse-ios" in lowered:
        return "ios"
    if "mozilla" in lowered or "chrome" in lowered or "safari" in lowered:
        return "web"
    return "other"


@router.post("/{session_id}/inputs-multipart", response_model=SessionInputResponse)
async def create_session_input_with_attachments(
    session_id: str,
    text: str = Form("", max_length=10000),
    intent: str = Form(INPUT_INTENT_AUTO),
    client_request_id: str | None = Form(None, max_length=64),
    attachments: List[UploadFile] = File(...),
    user_agent: str | None = Header(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> SessionInputResponse:
    """Send a user input with one or more image attachments.

    v1 only supports the ``auto`` intent. ``steer`` would need the live
    steer chain to accept attachments and would race the dispatch lock
    that this route already acquires for the regular send path.
    Queue-with-attachments is also rejected because the queued-input
    drain path doesn't load attachments yet.
    """
    client_label = _client_label_from_user_agent(user_agent)

    def _record_outcome(outcome: str) -> None:
        session_input_attachments_total.labels(client=client_label, outcome=outcome).inc()

    if not attachments:
        _record_outcome("rejected_empty")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="multipart input requires at least one attachment",
        )
    if intent != INPUT_INTENT_AUTO:
        _record_outcome("rejected_intent")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"intent {intent!r} not supported with attachments",
        )
    try:
        _validate_attachments(attachments)
    except HTTPException:
        _record_outcome("rejected_validation")
        raise

    # Read every upload + check size before we touch the DB. If a later
    # attachment is too large, we don't want a half-stored input in
    # ``delivering`` state with earlier blobs orphaned on disk.
    upload_payloads: list[tuple[UploadFile, bytes]] = []
    for upload in attachments:
        data = await upload.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            _record_outcome("rejected_oversize")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"attachment {upload.filename!r} exceeds "
                    f"{MAX_ATTACHMENT_BYTES // 1024 // 1024}MB"
                ),
            )
        upload_payloads.append((upload, data))

    try:
        source_session = _load_session_for_continuation(db, session_id)
    except HTTPException:
        _record_outcome("rejected_session")
        raise
    try:
        _assert_live_session_send_available(db, source_session, owner_id=current_user.id)
    except HTTPException:
        _record_outcome("rejected_live_control")
        raise

    capabilities = current_session_capabilities(db, source_session, owner_id=current_user.id)
    transport = (capabilities.managed_transport.value if capabilities.managed_transport else "")
    if transport != "codex_app_server":
        _record_outcome("rejected_capability")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="image attach is only supported on codex sessions",
        )

    request_id = (client_request_id or "").strip() or uuid.uuid4().hex
    delivery_request_id = uuid.uuid4().hex
    lock_scope_id = str(source_session.thread_root_session_id or source_session.id)

    # We acquire the dispatch lock before persisting anything so a second
    # request for the same session can't race the blob writes.
    lock = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder=delivery_request_id,
        ttl_seconds=300,
    )
    if not lock:
        _record_outcome("rejected_lock")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another dispatch is in flight for this session; try again",
        )

    stored_refs: list[dict] = []
    try:
        row = create_session_input(
            db,
            session_id=source_session.id,
            text=text,
            owner_id=current_user.id,
            intent=intent,
            status=INPUT_STATUS_DELIVERING,
            client_request_id=request_id,
            delivery_request_id=delivery_request_id,
        )
        for upload, data in upload_payloads:
            stored = store_attachment_blob(
                db,
                session_input=row,
                mime_type=upload.content_type,
                data=data,
                original_filename=upload.filename,
                original_byte_size=len(data),
            )
            stored_refs.append(
                _attachment_ref_for_engine(
                    session_id=str(source_session.id),
                    input_id=int(row.id),
                    stored=stored,
                )
            )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if "row" in locals():
            mark_failed(db, int(row.id), error="attachment store rejected")
        _record_outcome("store_rejected")
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if "row" in locals():
            mark_failed(db, int(row.id), error=f"attachment store failed: {exc}")
        logger.exception("attachment upload failed for session %s", source_session.id)
        _record_outcome("store_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to store attachments",
        ) from exc

    try:
        dispatch_response = await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=current_user.id,
            message=text,
            request_id=delivery_request_id,
            lock_scope_id=lock_scope_id,
            db=db,
            session_input_id=int(row.id),
            attachments=stored_refs,
        )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        mark_failed(db, int(row.id), error="dispatch rejected")
        _record_outcome("dispatch_rejected")
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        mark_failed(db, int(row.id), error=str(exc)[:200])
        logger.exception("attachment dispatch failed for session %s", source_session.id)
        _record_outcome("dispatch_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"dispatch failed: {exc}",
        ) from exc

    dispatch_status = int(getattr(dispatch_response, "status_code", 200) or 200)
    if dispatch_status >= 400:
        mark_failed(db, int(row.id), error=f"dispatch returned {dispatch_status}")
        _record_outcome("dispatch_error")
        raise HTTPException(
            status_code=dispatch_status,
            detail=f"managed local dispatch returned {dispatch_status}",
        )

    mark_delivered(db, int(row.id))

    attachments_list = list_attachments_for_input(db, int(row.id))
    for stored in attachments_list:
        session_input_attachment_bytes.observe(int(stored.byte_size))
    _record_outcome("delivered")
    logger.info(
        "session_input_attachments_uploaded session=%s input=%d client=%s count=%d total_bytes=%d",
        source_session.id,
        int(row.id),
        client_label,
        len(attachments_list),
        sum(a.byte_size for a in attachments_list),
    )

    return SessionInputResponse(
        outcome="sent",
        input_id=int(row.id),
        client_request_id=row.client_request_id,
        intent=row.intent,
        queued=[],
    )


@agents_router.get(
    "/{session_id}/inputs/{input_id}/attachments/{attachment_id}/blob",
)
async def fetch_attachment_blob(
    session_id: str,
    input_id: int,
    attachment_id: str,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> StreamingResponse:
    """Stream a single attachment blob to the engine.

    Always returns 404 on any cross-row mismatch (wrong session, wrong
    input). The engine still verifies sha256 before handing the path to
    Codex — the 404 is a defense-in-depth boundary, not a primary
    integrity contract.
    """
    try:
        attach_uuid = uuid.UUID(attachment_id)
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        session_input_attachment_blob_fetches_total.labels(outcome="bad_uuid").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from exc

    if device_token is None:
        # auth_disabled mode: still require a valid path; the framework already
        # enforces single-tenant.
        pass

    row = get_attachment(db, attach_uuid)
    if row is None or row.session_id != session_uuid or int(row.session_input_id) != input_id:
        session_input_attachment_blob_fetches_total.labels(outcome="not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    blob_path: Path = absolute_blob_path(row)
    if not blob_path.exists():
        logger.warning("attachment row %s exists but blob is missing at %s", row.id, blob_path)
        session_input_attachment_blob_fetches_total.labels(outcome="blob_missing").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    session_input_attachment_blob_fetches_total.labels(outcome="served").inc()

    def _iter_blob():
        with blob_path.open("rb") as fh:
            while chunk := fh.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _iter_blob(),
        media_type=row.mime_type,
        headers={
            "X-Attachment-Sha256": row.sha256,
            "X-Attachment-Bytes": str(int(row.byte_size)),
            "Content-Length": str(int(row.byte_size)),
        },
    )
