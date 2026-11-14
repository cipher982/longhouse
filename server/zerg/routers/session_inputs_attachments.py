"""Multipart input + attachment blob fetch endpoints.

The browser/iOS composer hits ``POST /api/sessions/{id}/inputs-multipart``
when the user attaches one or more images. This route only reads and
validates the files; delivery (SEND, QUEUE or STEER, Helm or Console) is the
same code the JSON ``POST /sessions/{id}/input`` route runs
(``_create_catalog_session_input_response``).

The Machine Agent fetches the blob bytes through
``GET /api/agents/sessions/{sid}/inputs/{iid}/attachments/{aid}/blob``
using the standard ``X-Agents-Token`` header and a per-session 404 boundary
so a leaked attachment id can never read across sessions.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any
from typing import List

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import UploadFile
from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from zerg.auth.caller import Caller
from zerg.auth.media_url_tokens import parse_media_url_token
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.browser_route_auth import get_current_browser_route_caller
from zerg.dependencies.browser_route_auth import get_optional_browser_route_caller
from zerg.dependencies.form_post_origin import reject_cross_origin_form_post
from zerg.dependencies.request_db import no_request_db
from zerg.metrics import session_input_attachment_blob_fetches_total
from zerg.metrics import session_input_attachment_bytes
from zerg.metrics import session_input_attachments_total
from zerg.models.device_token import DeviceToken
from zerg.routers.session_chat import SessionInputRequest
from zerg.routers.session_chat import SessionInputResponse
from zerg.routers.session_chat import _create_catalog_session_input_response
from zerg.routers.session_chat import _input_error_detail
from zerg.services.live_session_inputs import load_live_input_receipt_by_client_request
from zerg.services.session_chat_impl import _load_session_for_continuation
from zerg.services.session_chat_impl import _resolve_agents_owner_id
from zerg.services.session_input_attachments import ALLOWED_MIME_TYPES
from zerg.services.session_input_attachments import MAX_ATTACHMENT_BYTES
from zerg.services.session_input_attachments import MAX_ATTACHMENTS_PER_INPUT
from zerg.services.session_input_attachments import get_catalog_attachment
from zerg.services.session_input_attachments import list_catalog_attachments
from zerg.services.session_input_links import input_attachment_token_subject
from zerg.services.session_input_uploads import InputUpload
from zerg.services.session_input_uploads import InputUploads
from zerg.services.session_inputs import INPUT_INTENT_AUTO
from zerg.services.session_inputs import INPUT_INTENT_QUEUE
from zerg.services.session_inputs import INPUT_INTENT_STEER
from zerg.services.session_preferences import stamp_owner_input_soon

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])


def _validate_attachments(files: List[UploadFile]) -> None:
    if len(files) > MAX_ATTACHMENTS_PER_INPUT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_input_error_detail(
                "too_many_attachments",
                f"too many attachments (max {MAX_ATTACHMENTS_PER_INPUT})",
            ),
        )
    for upload in files:
        if upload.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_input_error_detail(
                    "unsupported_attachment_type",
                    f"unsupported attachment type: {upload.content_type}",
                ),
            )


def _image_signature_matches(mime_type: str, data: bytes) -> bool:
    """Reject a declared image type whose bytes are not that image family."""
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _validate_attachment_bytes(mime_type: str | None, data: bytes) -> None:
    if not mime_type or not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_input_error_detail("empty_attachment", "attachments must not be empty"),
        )
    if not _image_signature_matches(mime_type, data):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=_input_error_detail(
                "attachment_signature_mismatch",
                f"attachment bytes do not match declared type: {mime_type}",
            ),
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
    request: Request,
    text: str = Form("", max_length=10000),
    intent: str = Form(INPUT_INTENT_AUTO),
    client_request_id: str = Form(..., min_length=1, max_length=64),
    model: str | None = Form(None),
    attachments: List[UploadFile] = File(...),
    user_agent: str | None = Header(default=None),
    db: Session | None = Depends(no_request_db),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> SessionInputResponse:
    """Send, queue or steer a user input that carries one or more images.

    Any intent the JSON route accepts (``auto``, ``queue``, ``steer``) on any
    session that accepts images; text may be empty.
    """

    try:
        return await _create_session_input_with_attachments(
            session_id=session_id,
            request=request,
            text=text,
            intent=intent,
            client_request_id=client_request_id,
            model=model,
            attachments=attachments,
            user_agent=user_agent,
            db=db,
            current_user=current_user,
        )
    finally:
        # The owner sent this from a composer; Recent sorts by it. Keyed to the
        # receipt the route created, so a request rejected before one existed
        # stamps nothing, and a raise after persisting still stamps.
        stamp_owner_input_soon(session_id, owner_id=int(current_user.id), client_request_id=client_request_id.strip())


async def _create_session_input_with_attachments(
    *,
    session_id: str,
    request: Request,
    text: str,
    intent: str,
    client_request_id: str,
    model: str | None,
    attachments: List[UploadFile],
    user_agent: str | None,
    db: Session | None,
    current_user: Caller,
) -> SessionInputResponse:
    client_label = _client_label_from_user_agent(user_agent)
    request_id = client_request_id.strip()

    def _rejected_detail(detail: object, *, code: str = "input_rejected") -> dict[str, Any]:
        payload = dict(detail) if isinstance(detail, dict) else {"message": str(detail)}
        message = str(payload.get("message") or payload.get("error") or code)
        return _input_error_detail(
            str(payload.get("error_code") or payload.get("code") or code),
            message,
            disposition="rejected",
            client_request_id=request_id,
        )

    def _record_outcome(outcome: str) -> None:
        session_input_attachments_total.labels(client=client_label, outcome=outcome).inc()

    try:
        reject_cross_origin_form_post(request)
    except HTTPException as exc:
        _record_outcome("rejected_cross_origin")
        raise HTTPException(status_code=exc.status_code, detail=_rejected_detail(exc.detail, code="cross_origin_rejected")) from exc

    if not request_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_rejected_detail("client_request_id must not be blank", code="invalid_client_request_id"),
        )
    if not attachments:
        _record_outcome("rejected_empty")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_rejected_detail("multipart input requires at least one attachment", code="empty_attachments"),
        )
    if intent not in (INPUT_INTENT_AUTO, INPUT_INTENT_QUEUE, INPUT_INTENT_STEER):
        _record_outcome("rejected_intent")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_rejected_detail(f"unknown intent: {intent!r}", code="invalid_intent"),
        )
    try:
        _validate_attachments(attachments)
    except HTTPException as exc:
        _record_outcome("rejected_validation")
        raise HTTPException(status_code=exc.status_code, detail=_rejected_detail(exc.detail)) from exc

    # Read and check every file before anything is persisted, so a bad later
    # file never leaves a half-stored input behind.
    files: list[InputUpload] = []
    for upload in attachments:
        data = await upload.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            _record_outcome("rejected_oversize")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_rejected_detail(
                    f"attachment {upload.filename!r} exceeds {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB",
                    code="attachment_oversize",
                ),
            )
        try:
            _validate_attachment_bytes(upload.content_type, data)
        except HTTPException as exc:
            _record_outcome("rejected_signature")
            raise HTTPException(status_code=exc.status_code, detail=_rejected_detail(exc.detail)) from exc
        files.append(InputUpload(filename=upload.filename, mime_type=str(upload.content_type), data=data))
    uploads = InputUploads.build(text=text, intent=intent, files=files)

    try:
        source_session = _load_session_for_continuation(db, session_id, owner_id=int(current_user.id))
    except HTTPException as exc:
        _record_outcome("rejected_session")
        raise HTTPException(status_code=exc.status_code, detail=_rejected_detail(exc.detail, code="session_unavailable")) from exc

    # The JSON schema requires text; an image-only input is valid here.
    body = SessionInputRequest.model_construct(
        text=text,
        intent=intent,
        client_request_id=request_id,
        report_id=None,
        model=model,
    )
    try:
        response = await _create_catalog_session_input_response(
            source_session=source_session,
            owner_id=int(current_user.id),
            body=body,
            db=db,
            uploads=uploads,
        )
    except HTTPException as exc:
        _record_outcome(f"error_{exc.status_code}")
        raise
    _record_outcome(str(response.outcome))
    for upload in files:
        session_input_attachment_bytes.observe(len(upload.data))
    logger.info(
        "session_input_attachments session=%s input=%s client=%s intent=%s outcome=%s count=%d total_bytes=%d",
        source_session.id,
        response.live_input_id,
        client_label,
        intent,
        response.outcome,
        len(files),
        uploads.total_bytes,
    )
    return response


@router.get("/{session_id}/input-attachments/{client_request_id}/{index}")
async def fetch_input_attachment_preview(
    session_id: str,
    client_request_id: str,
    index: int,
    mt: str | None = Query(None, description="Signed read credential minted into the thumbnail URL."),
    ambient: Caller | None = Depends(get_optional_browser_route_caller),
    db: Session | None = Depends(no_request_db),
) -> StreamingResponse:
    """Stream one image the signed-in owner attached to a send, for the transcript thumbnail.

    Addressed by the send's client request id and the image's upload position,
    which is what a transcript row already carries. Bytes expire with the
    attachment (24 h); after that this is a plain 404 and the row keeps its
    "N images attached" label.
    """
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from exc
    if index < 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    # The thumbnail renders inside a document, which cannot send a header: the
    # URL's signed credential (bound to this one image) or an ambient browser
    # credential is the only authority available.
    signed_owner = parse_media_url_token(mt, sha256=input_attachment_token_subject(session_uuid, client_request_id, index)) if mt else None
    if signed_owner is not None:
        owner_id = signed_owner
    elif ambient is not None:
        owner_id = int(ambient.id)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        receipt = await load_live_input_receipt_by_client_request(
            owner_id=owner_id, session_id=session_uuid, client_request_id=client_request_id
        )
        attachments = (
            await list_catalog_attachments(owner_id=owner_id, session_id=session_uuid, input_receipt_id=receipt.id)
            if receipt is not None
            else []
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning("attachment preview lookup failed", exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="attachment catalog unavailable") from exc
    if index >= len(attachments):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    row = attachments[index]
    if row.session_id != session_uuid or not row.blob_path.is_file() or not row.mime_type.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    def _iter_blob():
        with row.blob_path.open("rb") as fh:
            while chunk := fh.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _iter_blob(),
        media_type=row.mime_type,
        headers={
            "Content-Length": str(int(row.byte_size)),
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@agents_router.get(
    "/{session_id}/inputs/{input_id}/attachments/{attachment_id}/blob",
)
async def fetch_attachment_blob(
    session_id: str,
    input_id: str,
    attachment_id: str,
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
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

    # The blob is transcript-adjacent user content, so it is read as the caller,
    # never as a derived "probably the only user" identity. This fails closed
    # when the token names an owner who no longer exists.
    owner_id = _resolve_agents_owner_id(db, device_token)

    try:
        stored = await get_catalog_attachment(
            owner_id=int(owner_id),
            session_id=session_uuid,
            input_receipt_id=input_id,
            attachment_id=attach_uuid,
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning("catalog attachment lookup failed", exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="attachment catalog unavailable") from exc
    row = stored
    if row is None or row.session_id != session_uuid:
        session_input_attachment_blob_fetches_total.labels(outcome="not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    blob_path: Path = row.blob_path
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
