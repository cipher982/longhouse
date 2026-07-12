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

import zerg.database as database_module
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
from zerg.services.live_session_inputs import record_live_input_receipt_best_effort
from zerg.services.managed_provider_contracts import managed_transport_for_control_plane
from zerg.services.session_chat_impl import _assert_live_session_send_available
from zerg.services.session_chat_impl import _build_managed_local_chat_response
from zerg.services.session_chat_impl import _load_session_for_continuation
from zerg.services.session_current_control import current_session_capabilities
from zerg.services.session_input_attachments import ALLOWED_MIME_TYPES
from zerg.services.session_input_attachments import MAX_ATTACHMENT_BYTES
from zerg.services.session_input_attachments import MAX_ATTACHMENTS_PER_INPUT
from zerg.services.session_input_attachments import StoredAttachment
from zerg.services.session_input_attachments import get_attachment
from zerg.services.session_input_attachments import get_catalog_attachment
from zerg.services.session_input_attachments import list_attachments_for_input
from zerg.services.session_input_attachments import read_path_for_attachment
from zerg.services.session_input_attachments import store_attachment_blob
from zerg.services.session_input_attachments import store_catalog_attachment_blob
from zerg.services.session_inputs import INPUT_INTENT_AUTO
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import create_session_input
from zerg.services.session_inputs import mark_delivered
from zerg.services.session_inputs import mark_failed
from zerg.services.session_kernel_projection import session_lock_scope_id
from zerg.services.session_locks import session_lock_manager
from zerg.session_execution_home import ManagedSessionTransport

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])


def _no_catalog_db():
    yield None


_attachment_db_dependency = _no_catalog_db if database_module.live_catalog_enabled() else get_db


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


def _catalog_codex_transport_available(source_session) -> bool:
    facts = getattr(source_session, "catalog_facts", None)
    connections = facts.get("connections") if isinstance(facts, dict) else None
    if not isinstance(connections, list):
        return False
    return any(
        isinstance(connection, dict)
        and connection.get("state") == "attached"
        and connection.get("released_at") is None
        and managed_transport_for_control_plane(connection.get("control_plane")) == ManagedSessionTransport.CODEX_APP_SERVER
        for connection in connections
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


@router.post("/{session_id}/inputs-multipart", response_model=SessionInputResponse)
async def create_session_input_with_attachments(
    session_id: str,
    text: str = Form("", max_length=10000),
    intent: str = Form(INPUT_INTENT_AUTO),
    client_request_id: str | None = Form(None, max_length=64),
    attachments: List[UploadFile] = File(...),
    user_agent: str | None = Header(default=None),
    db: Session | None = Depends(_attachment_db_dependency),
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
                detail=(f"attachment {upload.filename!r} exceeds {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB"),
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

    if database_module.live_catalog_enabled():
        codex_transport = _catalog_codex_transport_available(source_session)
    else:
        capabilities = current_session_capabilities(db, source_session, owner_id=current_user.id)
        codex_transport = bool(capabilities.managed_transport and capabilities.managed_transport.value == "codex_app_server")
    if not codex_transport:
        _record_outcome("rejected_capability")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="image attach is only supported on codex sessions",
        )

    request_id = (client_request_id or "").strip() or uuid.uuid4().hex
    delivery_request_id = uuid.uuid4().hex
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another dispatch is in flight for this session; try again",
        )

    stored_refs: list[dict] = []
    catalog_receipt_id: str | None = None
    try:
        if database_module.live_catalog_enabled():
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
                delivery_request_id=delivery_request_id,
            )
            if catalog_receipt_id is None:
                raise RuntimeError("catalog input receipt is unavailable")
            input_identity: int | str = catalog_receipt_id
        else:
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
            input_identity = int(row.id)
        for upload, data in upload_payloads:
            if database_module.live_catalog_enabled():
                stored = await store_catalog_attachment_blob(
                    input_receipt_id=catalog_receipt_id,
                    owner_id=int(current_user.id),
                    session_id=source_session.id,
                    mime_type=upload.content_type,
                    data=data,
                    original_filename=upload.filename,
                    original_byte_size=len(data),
                )
            else:
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
                    input_id=input_identity,
                    stored=stored,
                )
            )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if catalog_receipt_id is not None:
            await _finish_catalog_receipt(
                receipt_id=catalog_receipt_id,
                delivery_request_id=delivery_request_id,
                error="attachment store rejected",
            )
        elif "row" in locals():
            mark_failed(db, int(row.id), error="attachment store rejected")
        _record_outcome("store_rejected")
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if catalog_receipt_id is not None:
            await _finish_catalog_receipt(
                receipt_id=catalog_receipt_id,
                delivery_request_id=delivery_request_id,
                error=f"attachment store failed: {exc}",
            )
        elif "row" in locals():
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
            session_input_id=(int(row.id) if not database_module.live_catalog_enabled() else None),
            attachments=stored_refs,
        )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if catalog_receipt_id is not None:
            await _finish_catalog_receipt(
                receipt_id=catalog_receipt_id,
                delivery_request_id=delivery_request_id,
                error="dispatch rejected",
            )
        else:
            mark_failed(db, int(row.id), error="dispatch rejected")
        _record_outcome("dispatch_rejected")
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if catalog_receipt_id is not None:
            await _finish_catalog_receipt(
                receipt_id=catalog_receipt_id,
                delivery_request_id=delivery_request_id,
                error=str(exc)[:200],
            )
        else:
            mark_failed(db, int(row.id), error=str(exc)[:200])
        logger.exception("attachment dispatch failed for session %s", source_session.id)
        _record_outcome("dispatch_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"dispatch failed: {exc}",
        ) from exc

    dispatch_status = int(getattr(dispatch_response, "status_code", 200) or 200)
    if dispatch_status >= 400:
        if catalog_receipt_id is not None:
            await _finish_catalog_receipt(
                receipt_id=catalog_receipt_id,
                delivery_request_id=delivery_request_id,
                error=f"dispatch returned {dispatch_status}",
            )
        else:
            mark_failed(db, int(row.id), error=f"dispatch returned {dispatch_status}")
        _record_outcome("dispatch_error")
        raise HTTPException(
            status_code=dispatch_status,
            detail=f"managed local dispatch returned {dispatch_status}",
        )

    uploaded_count = len(stored_refs)
    uploaded_bytes = sum(len(data) for _, data in upload_payloads)
    if catalog_receipt_id is not None:
        await _finish_catalog_receipt(receipt_id=catalog_receipt_id, delivery_request_id=delivery_request_id)
        attachments_list = []
    else:
        mark_delivered(db, int(row.id))
        attachments_list = list_attachments_for_input(db, int(row.id))
    for stored in attachments_list:
        session_input_attachment_bytes.observe(int(stored.byte_size))
    if catalog_receipt_id is not None:
        for _, data in upload_payloads:
            session_input_attachment_bytes.observe(len(data))
    _record_outcome("delivered")
    logger.info(
        "session_input_attachments_uploaded session=%s input=%s client=%s count=%d total_bytes=%d",
        source_session.id,
        catalog_receipt_id or int(row.id),
        client_label,
        uploaded_count,
        uploaded_bytes,
    )

    return SessionInputResponse(
        outcome="sent",
        input_id=(None if catalog_receipt_id else int(row.id)),
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
    db: Session | None = Depends(_attachment_db_dependency),
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

    if database_module.live_catalog_enabled():
        from zerg.services.catalog_read_gateway import active_owner_id

        owner_id = getattr(device_token, "owner_id", None) or active_owner_id()
        if owner_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
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
    else:
        try:
            legacy_input_id = int(input_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from exc
        row = get_attachment(db, attach_uuid)
        if row is not None and int(row.session_input_id) != legacy_input_id:
            row = None
    if row is None or row.session_id != session_uuid:
        session_input_attachment_blob_fetches_total.labels(outcome="not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    blob_path: Path = row.blob_path if database_module.live_catalog_enabled() else read_path_for_attachment(db, row)
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
