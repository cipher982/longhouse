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

import hashlib
import json
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
from fastapi import Request
from fastapi import UploadFile
from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from zerg.auth.caller import Caller
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.browser_route_auth import get_current_browser_route_caller
from zerg.dependencies.form_post_origin import reject_cross_origin_form_post
from zerg.dependencies.request_db import no_request_db
from zerg.metrics import session_input_attachment_blob_fetches_total
from zerg.metrics import session_input_attachment_bytes
from zerg.metrics import session_input_attachments_total
from zerg.models.device_token import DeviceToken
from zerg.routers.session_chat import QueuedInputSummary
from zerg.routers.session_chat import SessionInputResponse
from zerg.routers.session_chat import _augment_receipt_error
from zerg.routers.session_chat import _console_turn_failure
from zerg.routers.session_chat import _console_turn_outcome
from zerg.routers.session_chat import _console_turn_response
from zerg.routers.session_chat import _delivery_unknown_error
from zerg.routers.session_chat import _input_error_detail
from zerg.routers.session_chat import _live_queued_summary
from zerg.routers.session_chat import _live_receipt_outcome
from zerg.routers.session_chat import _runtime_draining_error
from zerg.routers.session_chat import _set_catalog_live_receipt_error
from zerg.services.input_attachments_support import attachments_supported
from zerg.services.live_session_inputs import LiveInputReceiptUnavailable
from zerg.services.live_session_inputs import load_live_input_receipt_by_client_request
from zerg.services.live_session_inputs import record_live_input_receipt_best_effort
from zerg.services.session_chat_impl import _assert_live_session_send_available
from zerg.services.session_chat_impl import _build_managed_local_chat_response
from zerg.services.session_chat_impl import _load_session_for_continuation
from zerg.services.session_chat_impl import _resolve_agents_owner_id
from zerg.services.session_input_attachments import ALLOWED_MIME_TYPES
from zerg.services.session_input_attachments import MAX_ATTACHMENT_BYTES
from zerg.services.session_input_attachments import MAX_ATTACHMENTS_PER_INPUT
from zerg.services.session_input_attachments import StoredAttachment
from zerg.services.session_input_attachments import delete_catalog_attachment_blobs
from zerg.services.session_input_attachments import get_catalog_attachment
from zerg.services.session_input_attachments import store_catalog_attachment_blob
from zerg.services.session_inputs import INPUT_INTENT_AUTO
from zerg.services.session_inputs import INPUT_INTENT_QUEUE
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_kernel_projection import session_lock_scope_id
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_preferences import stamp_owner_input_soon

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])


def _attachment_ref_for_engine(
    *,
    session_id: str,
    input_id: int | str,
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
        "blob_url": (f"/api/agents/sessions/{session_id}/inputs/{input_id}/attachments/{stored.id}/blob"),
    }


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


def _queued_summary_from_row(row) -> QueuedInputSummary:
    return QueuedInputSummary(
        id=int(row.id),
        text=row.body,
        intent=row.intent,
        status=row.status,
        disposition="accepted",
        delivery_status=row.status,
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


def _console_attachments_digest(upload_payloads: list[tuple[UploadFile, bytes]]) -> str:
    """Ordered (mime, sha256) digest a Console replay compares without re-reading blobs."""
    hasher = hashlib.sha256()
    hasher.update(b"longhouse-console-attachments-v1\0")
    for upload, data in upload_payloads:
        hasher.update((upload.content_type or "application/octet-stream").encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(data).hexdigest().encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


async def _enqueue_console_input_with_attachments(
    *,
    source_session,
    owner_id: int,
    text: str,
    client_request_id: str,
    model: str | None,
    upload_payloads: list[tuple[UploadFile, bytes]],
    record_outcome,
    intent: str = INPUT_INTENT_AUTO,
) -> SessionInputResponse:
    """Console path: store the blobs, then enqueue the turn with their refs.

    The turn record carries the refs, so a FIFO or reconnect dispatch after a
    restart still delivers them. A replay with the same ``client_request_id``
    stores nothing and passes only the digest; catalogd compares it against
    the stored turn and answers with the existing receipt or a conflict.
    """
    from zerg.services.console_turns import ConsoleTurnConflict
    from zerg.services.console_turns import ConsoleTurnUnavailable
    from zerg.services.console_turns import enqueue_catalog_console_turn

    if not attachments_supported(getattr(source_session, "provider", None), "console"):
        record_outcome("rejected_capability")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_input_error_detail(
                "attachments_unsupported",
                "This session's provider does not accept image attachments",
                disposition="rejected",
                client_request_id=client_request_id,
            ),
        )
    group_id: str | None = None
    existing_receipt = None

    async def cleanup_stored_group() -> None:
        if group_id is None:
            return
        try:
            await delete_catalog_attachment_blobs(
                owner_id=owner_id,
                session_id=source_session.id,
                input_receipt_id=group_id,
            )
        except Exception:
            logger.exception("console attachment cleanup failed for session %s", source_session.id)

    digest = _console_attachments_digest(upload_payloads)
    try:
        existing_receipt = await load_live_input_receipt_by_client_request(
            owner_id=owner_id,
            session_id=source_session.id,
            client_request_id=client_request_id,
        )
    except LiveInputReceiptUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_input_error_detail(
                "input_receipt_unknown",
                "The server could not confirm this operation; retry with the same client_request_id.",
                disposition="unknown",
                client_request_id=client_request_id,
            ),
        ) from exc
    stored_refs: list[dict] = []
    if existing_receipt is None:
        group_id = str(uuid.uuid4())
        try:
            for upload, data in upload_payloads:
                stored = await store_catalog_attachment_blob(
                    input_receipt_id=group_id,
                    owner_id=owner_id,
                    session_id=source_session.id,
                    mime_type=upload.content_type,
                    data=data,
                    original_filename=upload.filename,
                    original_byte_size=len(data),
                    allow_unbound=True,
                )
                stored_refs.append(_attachment_ref_for_engine(session_id=str(source_session.id), input_id=group_id, stored=stored))
        except Exception as exc:
            await cleanup_stored_group()
            logger.exception("console attachment upload failed for session %s", source_session.id)
            record_outcome("store_failed")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to store attachments") from exc
    try:
        enqueue_kwargs = {
            "owner_id": owner_id,
            "session_id": uuid.UUID(str(source_session.id)),
            "message": text,
            "client_request_id": client_request_id,
            "attachments": stored_refs,
            "attachments_digest": digest,
            "receipt_id": group_id,
        }
        if model is not None:
            enqueue_kwargs["model"] = model
        turn = await enqueue_catalog_console_turn(**enqueue_kwargs)
    except ConsoleTurnConflict as exc:
        await cleanup_stored_group()
        record_outcome("rejected_idempotency_conflict")
        conflict_receipt = existing_receipt
        if conflict_receipt is None:
            try:
                conflict_receipt = await load_live_input_receipt_by_client_request(
                    owner_id=owner_id,
                    session_id=source_session.id,
                    client_request_id=client_request_id,
                )
            except LiveInputReceiptUnavailable:
                conflict_receipt = None
        if conflict_receipt is not None:
            summary = _live_queued_summary(conflict_receipt)
            detail = _input_error_detail(
                "idempotency_conflict",
                str(exc),
                disposition="accepted",
                delivery_status=conflict_receipt.status,
                client_request_id=client_request_id,
                live_input_id=conflict_receipt.id,
                turn=summary.turn,
            )
        else:
            detail = _input_error_detail(
                "idempotency_conflict",
                str(exc),
                disposition="unknown",
                client_request_id=client_request_id,
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc
    except ConsoleTurnUnavailable as exc:
        if exc.code == "catalog_unavailable":
            record_outcome("dispatch_unknown")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_input_error_detail(
                    exc.code,
                    str(exc),
                    disposition="unknown",
                    client_request_id=client_request_id,
                ),
            ) from exc
        await cleanup_stored_group()
        record_outcome("rejected_unavailable")
        error_status = status.HTTP_404_NOT_FOUND if exc.code == "report_not_found" else status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=error_status,
            detail=_input_error_detail(
                exc.code,
                str(exc),
                disposition="rejected",
                client_request_id=client_request_id,
            ),
        ) from exc
    except Exception as exc:
        # catalogd may have committed the turn before its response was lost;
        # retain the unbound group for the receipt reaper and let an idempotent
        # retry recover the same operation.
        logger.exception("console attachment dispatch outcome unknown for session %s", source_session.id)
        record_outcome("dispatch_unknown")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_input_error_detail(
                "input_receipt_unknown",
                "The server could not confirm this operation; retry with the same client_request_id.",
                disposition="unknown",
                client_request_id=client_request_id,
            ),
        ) from exc
    if group_id is not None and not turn.created:
        # Another request won the idempotency race. Its turn owns the durable
        # attachment group; discard this request's duplicate upload.
        await cleanup_stored_group()
        group_id = None
    if turn.state == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_input_error_detail(
                "input_cancelled",
                "This Console input was cancelled.",
                disposition="accepted",
                delivery_status="cancelled",
                client_request_id=client_request_id,
                live_input_id=str(turn.receipt_id) if getattr(turn, "receipt_id", None) is not None else None,
                turn=_console_turn_response(turn),
            ),
        )
    if turn.error and turn.error_code not in {
        "turn_start_ambiguous",
        "turn_start_outcome_unknown",
        "attachment_stage_outcome_unknown",
    }:
        if turn.created:
            await cleanup_stored_group()
            group_id = None
        record_outcome("dispatch_failed")
        raise _console_turn_failure(turn, client_request_id=client_request_id)
    if turn.error:
        record_outcome("dispatch_deferred")
    else:
        record_outcome("accepted_console")
    return SessionInputResponse(
        outcome=_console_turn_outcome(turn.state),
        disposition="accepted",
        input_id=None,
        live_input_id=str(turn.receipt_id) if getattr(turn, "receipt_id", None) is not None else None,
        client_request_id=client_request_id,
        turn=_console_turn_response(turn),
        intent=intent,
        queued=[],
    )


async def _finish_catalog_receipt(*, receipt_id: str, delivery_request_id: str, error: str | None = None) -> None:
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise RuntimeError("catalogd is unavailable")
    await catalogd.call(
        "session.input.finish.v2",
        {
            "receipt_id": receipt_id,
            "delivery_request_id": delivery_request_id,
            "status": "failed" if error else "delivered",
            "error": error[:500] if error else None,
        },
        timeout_seconds=1.0,
    )


async def _cleanup_catalog_attachment_group(*, owner_id: int, session_id: str, receipt_id: str) -> None:
    try:
        await delete_catalog_attachment_blobs(
            owner_id=owner_id,
            session_id=session_id,
            input_receipt_id=receipt_id,
        )
    except Exception:
        logger.exception("attachment cleanup failed for session %s receipt %s", session_id, receipt_id)


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
    response = await _create_session_input_with_attachments(
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
    # The owner sent this from a composer; Recent sorts by it. Only after the
    # route accepted it, keyed to the receipt it created.
    stamp_owner_input_soon(session_id, owner_id=int(current_user.id), client_request_id=client_request_id.strip())
    return response


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
    """Send a user input with one or more image attachments.

    ``auto`` everywhere; ``queue`` only on Console sessions, where it is the
    same FIFO enqueue as ``auto`` and the turn record carries the image refs,
    so a user can attach while a turn runs. ``steer`` would need the live
    steer chain to accept attachments and would race the dispatch lock
    that this route already acquires for the regular send path. Helm
    queue-with-attachments stays rejected: its queued-input drain path does
    not load attachments.
    """
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

    if not attachments:
        _record_outcome("rejected_empty")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_rejected_detail("multipart input requires at least one attachment", code="empty_attachments"),
        )
    if intent not in (INPUT_INTENT_AUTO, INPUT_INTENT_QUEUE):
        _record_outcome("rejected_intent")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_rejected_detail(f"intent {intent!r} not supported with attachments", code="invalid_intent"),
        )
    try:
        _validate_attachments(attachments)
    except HTTPException as exc:
        _record_outcome("rejected_validation")
        raise HTTPException(status_code=exc.status_code, detail=_rejected_detail(exc.detail)) from exc

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
        upload_payloads.append((upload, data))

    payload_hasher = hashlib.sha256()
    payload_hasher.update(b"longhouse-input-payload-v1\0")
    payload_hasher.update(text.encode("utf-8"))
    payload_hasher.update(b"\0")
    payload_hasher.update(intent.encode("utf-8"))
    for upload, data in upload_payloads:
        for component in (
            upload.filename or "",
            upload.content_type or "application/octet-stream",
            str(len(data)),
        ):
            payload_hasher.update(component.encode("utf-8"))
            payload_hasher.update(b"\0")
        payload_hasher.update(data)
    payload_digest = payload_hasher.hexdigest()
    try:
        source_session = _load_session_for_continuation(db, session_id, owner_id=int(current_user.id))
    except HTTPException as exc:
        _record_outcome("rejected_session")
        raise HTTPException(status_code=exc.status_code, detail=_rejected_detail(exc.detail, code="session_unavailable")) from exc
    request_id = client_request_id.strip()
    if not request_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_rejected_detail("client_request_id must not be blank", code="invalid_client_request_id"),
        )

    if getattr(source_session, "command_family", None) == "console_turn":
        return await _enqueue_console_input_with_attachments(
            source_session=source_session,
            owner_id=int(current_user.id),
            text=text,
            client_request_id=request_id,
            model=model,
            upload_payloads=upload_payloads,
            record_outcome=_record_outcome,
            intent=intent,
        )
    if intent != INPUT_INTENT_AUTO:
        _record_outcome("rejected_intent")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_rejected_detail(f"intent {intent!r} not supported with attachments", code="invalid_intent"),
        )

    try:
        _assert_live_session_send_available(db, source_session, owner_id=current_user.id)
    except HTTPException as exc:
        _record_outcome("rejected_live_control")
        raise HTTPException(status_code=exc.status_code, detail=_rejected_detail(exc.detail, code="session_unavailable")) from exc

    if not attachments_supported(getattr(source_session, "provider", None), "helm"):
        _record_outcome("rejected_capability")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_rejected_detail(
                "This session's provider does not accept image attachments",
                code="attachments_unsupported",
            ),
        )
    try:
        existing_receipt = await load_live_input_receipt_by_client_request(
            owner_id=int(current_user.id),
            session_id=source_session.id,
            client_request_id=request_id,
        )
    except LiveInputReceiptUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_input_error_detail(
                "input_receipt_unknown",
                "The server could not confirm this operation; retry with the same client_request_id.",
                disposition="unknown",
                client_request_id=request_id,
            ),
        ) from exc
    runtime_replay = False
    if existing_receipt is not None:
        if existing_receipt.payload_digest != payload_digest:
            _record_outcome("rejected_idempotency_conflict")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_augment_receipt_error(
                    {"error_code": "input_conflict", "reason": "different_payload"},
                    client_request_id=request_id,
                    live_input_id=existing_receipt.id,
                    disposition="accepted",
                    delivery_status=existing_receipt.status,
                ),
            )
        is_delivery_unknown = existing_receipt.status == "failed" and _delivery_unknown_error(existing_receipt.error_json)
        runtime_replay = existing_receipt.status == INPUT_STATUS_DELIVERING and _runtime_draining_error(existing_receipt.error_json)
        if existing_receipt.status in {"failed", "cancelled"} and not is_delivery_unknown:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_input_error_detail(
                    "input_already_rejected",
                    "This submitted input already reached a terminal delivery state.",
                    disposition="accepted",
                    delivery_status=existing_receipt.status,
                    client_request_id=request_id,
                    live_input_id=existing_receipt.id,
                ),
            )
        if not runtime_replay:
            return SessionInputResponse(
                outcome=_live_receipt_outcome(existing_receipt),
                input_id=None,
                live_input_id=existing_receipt.id,
                client_request_id=request_id,
                intent=existing_receipt.intent,
                queued=[],
            )
        if not str(existing_receipt.delivery_request_id or "").strip():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_augment_receipt_error(
                    {
                        "error_code": "input_receipt_unknown",
                        "message": "The server could not confirm this operation; retry with the same client_request_id.",
                    },
                    client_request_id=request_id,
                    live_input_id=existing_receipt.id,
                    disposition="unknown",
                    delivery_status="unknown",
                ),
            )
    delivery_request_id = str(existing_receipt.delivery_request_id) if runtime_replay and existing_receipt is not None else uuid.uuid4().hex
    lock_scope_id = session_lock_scope_id(source_session.id)

    # We acquire the dispatch lock before persisting anything so a second
    # request for the same session can't race the blob writes.
    lock = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder=delivery_request_id,
        ttl_seconds=300,
    )
    if not lock:
        _record_outcome("rejected_lock")
        detail = (
            _augment_receipt_error(
                {"error_code": "input_dispatch_in_flight", "message": "another dispatch is in flight for this session; try again"},
                client_request_id=request_id,
                live_input_id=existing_receipt.id,
                disposition="accepted",
                delivery_status=existing_receipt.status,
            )
            if existing_receipt is not None
            else _input_error_detail(
                "input_dispatch_in_flight",
                "another dispatch is in flight for this session; try again",
                disposition="rejected",
                client_request_id=request_id,
            )
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    if runtime_replay:
        try:
            current_receipt = await load_live_input_receipt_by_client_request(
                owner_id=int(current_user.id),
                session_id=source_session.id,
                client_request_id=request_id,
            )
        except LiveInputReceiptUnavailable as exc:
            await session_lock_manager.release(lock_scope_id, delivery_request_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "input_receipt_unknown",
                    "message": "The server could not confirm this operation; retry with the same client_request_id.",
                },
            ) from exc
        if current_receipt is None:
            await session_lock_manager.release(lock_scope_id, delivery_request_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "input_receipt_unknown",
                    "message": "The server could not confirm this operation; retry with the same client_request_id.",
                },
            )
        if current_receipt.payload_digest != payload_digest:
            await session_lock_manager.release(lock_scope_id, delivery_request_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "input_conflict",
                    "reason": "different_payload",
                    "existing_live_input_id": current_receipt.id,
                },
            )
        if current_receipt.status != INPUT_STATUS_DELIVERING or not _runtime_draining_error(current_receipt.error_json):
            await session_lock_manager.release(lock_scope_id, delivery_request_id)
            if current_receipt.status in {"delivered", "queued", "delivering"} or (
                current_receipt.status == "failed" and _delivery_unknown_error(current_receipt.error_json)
            ):
                return SessionInputResponse(
                    outcome=_live_receipt_outcome(current_receipt),
                    input_id=None,
                    live_input_id=current_receipt.id,
                    client_request_id=request_id,
                    intent=current_receipt.intent,
                    queued=[],
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "input_already_rejected",
                    "existing_live_input_id": current_receipt.id,
                    "status": current_receipt.status,
                },
            )
        if str(current_receipt.delivery_request_id or "").strip() != delivery_request_id:
            await session_lock_manager.release(lock_scope_id, delivery_request_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "input_receipt_unknown",
                    "message": "The server could not confirm this operation; retry with the same client_request_id.",
                },
            )
        existing_receipt = current_receipt
        claimed = await _set_catalog_live_receipt_error(
            receipt_id=existing_receipt.id,
            source_session=source_session,
            owner_id=int(current_user.id),
            text=text,
            intent=INPUT_INTENT_AUTO,
            client_request_id=request_id,
            delivery_request_id=delivery_request_id,
            payload_digest=payload_digest,
            error={
                "code": "delivery_unknown",
                "message": "Provider dispatch is in flight; do not replay until its outcome is known.",
            },
        )
        if not claimed:
            await session_lock_manager.release(lock_scope_id, delivery_request_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "input_receipt_unknown",
                    "message": "The server could not confirm this operation; retry with the same client_request_id.",
                },
            )

    stored_refs: list[dict] = []
    catalog_receipt_id: str | None = None
    try:
        if runtime_replay and existing_receipt is not None:
            catalog_receipt_id = existing_receipt.id
        else:
            catalog_receipt_id = await record_live_input_receipt_best_effort(
                owner_id=int(current_user.id),
                session_id=source_session.id,
                provider=str(source_session.provider or "codex"),
                device_id=str(source_session.device_id or "").strip() or None,
                thread_id=source_session.primary_thread_id,
                text=text,
                intent=intent,
                status=INPUT_STATUS_DELIVERING,
                client_request_id=request_id,
                payload_digest=payload_digest,
                delivery_request_id=delivery_request_id,
            )
        if catalog_receipt_id is None:
            raise RuntimeError("catalog input receipt is unavailable")
        input_identity = catalog_receipt_id
        for upload, data in upload_payloads:
            stored = await store_catalog_attachment_blob(
                input_receipt_id=catalog_receipt_id,
                owner_id=int(current_user.id),
                session_id=source_session.id,
                mime_type=upload.content_type,
                data=data,
                original_filename=upload.filename,
                original_byte_size=len(data),
            )
            stored_refs.append(
                _attachment_ref_for_engine(
                    session_id=str(source_session.id),
                    input_id=input_identity,
                    stored=stored,
                )
            )
    except HTTPException as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if catalog_receipt_id is not None:
            await _finish_catalog_receipt(
                receipt_id=catalog_receipt_id,
                delivery_request_id=delivery_request_id,
                error="attachment store rejected",
            )
            await _cleanup_catalog_attachment_group(
                owner_id=int(current_user.id),
                session_id=str(source_session.id),
                receipt_id=catalog_receipt_id,
            )
        _record_outcome("store_rejected")
        if catalog_receipt_id is None:
            raise
        raise HTTPException(
            status_code=exc.status_code,
            detail=_augment_receipt_error(
                exc.detail,
                client_request_id=request_id,
                live_input_id=catalog_receipt_id,
                delivery_status="failed",
            ),
        ) from exc
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if catalog_receipt_id is not None:
            await _finish_catalog_receipt(
                receipt_id=catalog_receipt_id,
                delivery_request_id=delivery_request_id,
                error=f"attachment store failed: {exc}",
            )
            await _cleanup_catalog_attachment_group(
                owner_id=int(current_user.id),
                session_id=str(source_session.id),
                receipt_id=catalog_receipt_id,
            )
        logger.exception("attachment upload failed for session %s", source_session.id)
        _record_outcome("store_failed")
        if catalog_receipt_id is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_input_error_detail(
                    "input_receipt_unknown",
                    "The server could not persist this operation; retry with the same client_request_id.",
                    disposition="unknown",
                    client_request_id=request_id,
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_augment_receipt_error(
                "failed to store attachments",
                client_request_id=request_id,
                live_input_id=catalog_receipt_id,
                delivery_status="failed",
            ),
        ) from exc

    try:
        dispatch_response = await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=current_user.id,
            message=text,
            request_id=delivery_request_id,
            lock_scope_id=lock_scope_id,
            db=db,
            session_input_id=None,
            attachments=stored_refs,
        )
    except HTTPException as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        await _finish_catalog_receipt(
            receipt_id=catalog_receipt_id,
            delivery_request_id=delivery_request_id,
            error="dispatch rejected",
        )
        await _cleanup_catalog_attachment_group(
            owner_id=int(current_user.id),
            session_id=str(source_session.id),
            receipt_id=catalog_receipt_id,
        )
        _record_outcome("dispatch_rejected")
        raise HTTPException(
            status_code=exc.status_code,
            detail=_augment_receipt_error(
                exc.detail,
                client_request_id=request_id,
                live_input_id=catalog_receipt_id,
                delivery_status="failed",
            ),
        ) from exc
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        unknown_error = f"Provider dispatch outcome is unknown: {exc}"[:500]
        marked = await _set_catalog_live_receipt_error(
            receipt_id=catalog_receipt_id,
            source_session=source_session,
            owner_id=int(current_user.id),
            text=text,
            intent=INPUT_INTENT_AUTO,
            client_request_id=request_id,
            delivery_request_id=delivery_request_id,
            payload_digest=payload_digest,
            error={"code": "delivery_unknown", "message": unknown_error},
        )
        if not marked:
            unknown_error = "The server could not confirm this operation; retry with the same client_request_id."
        logger.exception("attachment dispatch outcome unknown for session %s", source_session.id)
        _record_outcome("dispatch_unknown")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                _augment_receipt_error(
                    {"error_code": "delivery_unknown", "message": unknown_error},
                    client_request_id=request_id,
                    live_input_id=catalog_receipt_id,
                    disposition="accepted" if marked else "unknown",
                    delivery_status="unknown",
                )
                if marked
                else _input_error_detail(
                    "input_receipt_unknown",
                    unknown_error,
                    disposition="unknown",
                    client_request_id=request_id,
                )
            ),
        ) from exc

    dispatch_status = int(getattr(dispatch_response, "status_code", 200) or 200)
    if dispatch_status >= 400:
        try:
            payload = json.loads(getattr(dispatch_response, "body", b"{}") or b"{}")
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("error_code") in {"runtime_draining", "runtime_restarting"}:
            marked = await _set_catalog_live_receipt_error(
                receipt_id=catalog_receipt_id,
                source_session=source_session,
                owner_id=int(current_user.id),
                text=text,
                intent=INPUT_INTENT_AUTO,
                client_request_id=request_id,
                delivery_request_id=delivery_request_id,
                payload_digest=payload_digest,
                error=payload,
            )
            if not marked:
                payload = _input_error_detail(
                    "input_receipt_unknown",
                    "The server could not confirm this operation; retry with the same client_request_id.",
                    disposition="unknown",
                    client_request_id=request_id,
                )
            else:
                payload = _augment_receipt_error(
                    payload,
                    client_request_id=request_id,
                    live_input_id=catalog_receipt_id,
                    disposition="accepted",
                    delivery_status="delivering",
                )
            _record_outcome("dispatch_deferred")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)
        if isinstance(payload, dict) and _delivery_unknown_error(json.dumps(payload)):
            # Keep the receipt unknown.  A replay of a known pre-dispatch
            # refusal is terminalized with this same stable unknown code so
            # it cannot blindly redispatch; the first attempt remains in
            # delivering as before.
            unknown_error = str(payload.get("error") or payload.get("message") or "attachment delivery outcome is unknown")
            if runtime_replay:
                await _finish_catalog_receipt(
                    receipt_id=catalog_receipt_id,
                    delivery_request_id=delivery_request_id,
                    error=f"delivery_unknown: {unknown_error}",
                )
            else:
                await _set_catalog_live_receipt_error(
                    receipt_id=catalog_receipt_id,
                    source_session=source_session,
                    owner_id=int(current_user.id),
                    text=text,
                    intent=INPUT_INTENT_AUTO,
                    client_request_id=request_id,
                    delivery_request_id=delivery_request_id,
                    payload_digest=payload_digest,
                    error={"code": "delivery_unknown", "message": unknown_error},
                )
            _record_outcome("dispatch_unknown")
            raise HTTPException(
                status_code=dispatch_status,
                detail=_augment_receipt_error(
                    payload,
                    client_request_id=request_id,
                    live_input_id=catalog_receipt_id,
                    disposition="accepted",
                    delivery_status="unknown",
                ),
            )
        await _cleanup_catalog_attachment_group(
            owner_id=int(current_user.id),
            session_id=str(source_session.id),
            receipt_id=catalog_receipt_id,
        )
        await _finish_catalog_receipt(
            receipt_id=catalog_receipt_id,
            delivery_request_id=delivery_request_id,
            error=f"dispatch returned {dispatch_status}",
        )
        _record_outcome("dispatch_error")
        raise HTTPException(
            status_code=dispatch_status,
            detail=_augment_receipt_error(
                f"managed local dispatch returned {dispatch_status}",
                client_request_id=request_id,
                live_input_id=catalog_receipt_id,
                disposition="accepted",
                delivery_status="failed",
            ),
        )

    uploaded_count = len(stored_refs)
    uploaded_bytes = sum(len(data) for _, data in upload_payloads)
    await _finish_catalog_receipt(receipt_id=catalog_receipt_id, delivery_request_id=delivery_request_id)
    for _, data in upload_payloads:
        session_input_attachment_bytes.observe(len(data))
    _record_outcome("delivered")
    logger.info(
        "session_input_attachments_uploaded session=%s input=%s client=%s count=%d total_bytes=%d",
        source_session.id,
        catalog_receipt_id,
        client_label,
        uploaded_count,
        uploaded_bytes,
    )

    return SessionInputResponse(
        outcome="sent",
        input_id=None,
        live_input_id=catalog_receipt_id,
        client_request_id=request_id,
        intent=intent,
        queued=[],
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
