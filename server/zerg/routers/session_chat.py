"""Session control router for managed-local live-send and launch.

Enables live interaction with managed-local CLI sessions launched through
Longhouse. Per-session locks prevent concurrent send collisions.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from hashlib import blake2b

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import SessionInput
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.services.managed_local_launcher import ManagedLocalLaunchError
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import launch_managed_local_session_sync
from zerg.services.remote_session_launch import RemoteLaunchError
from zerg.services.remote_session_launch import RemoteLaunchParams
from zerg.services.remote_session_launch import launch_remote_session
from zerg.services.session_chat_impl import ManagedLocalSessionLaunchResponse
from zerg.services.session_chat_impl import SessionDraftReplyResponse
from zerg.services.session_chat_impl import SessionLockInfo
from zerg.services.session_chat_impl import _acquire_session_lock_or_raise
from zerg.services.session_chat_impl import _assert_live_session_send_available
from zerg.services.session_chat_impl import _authorize_live_send
from zerg.services.session_chat_impl import _build_managed_local_chat_response
from zerg.services.session_chat_impl import _build_managed_local_draft_reply_response
from zerg.services.session_chat_impl import _load_session_for_continuation
from zerg.services.session_chat_impl import _lock_scope_id_for_session
from zerg.services.session_chat_impl import _managed_local_launch_response
from zerg.services.session_chat_impl import _resolve_agents_owner_id
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_current_control import current_session_capabilities
from zerg.services.session_inputs import INPUT_INTENT_AUTO
from zerg.services.session_inputs import INPUT_INTENT_QUEUE
from zerg.services.session_inputs import INPUT_INTENT_STEER
from zerg.services.session_inputs import INPUT_STATUS_CANCELLED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import MAX_QUEUED_PER_SESSION
from zerg.services.session_inputs import cancel_queued_input
from zerg.services.session_inputs import count_queued
from zerg.services.session_inputs import create_session_input
from zerg.services.session_inputs import get_session_input
from zerg.services.session_inputs import list_recent_inputs
from zerg.services.session_inputs import mark_failed as _mark_input_failed
from zerg.services.session_inputs import retry_failed_input
from zerg.services.write_serializer import get_write_serializer
from zerg.session_loop_mode import SessionLoopMode
from zerg.session_loop_mode import coerce_session_loop_mode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SessionMessageRequest(BaseModel):
    """Request to send one message into an explicit session interaction path."""

    message: str = Field(..., min_length=1, max_length=10000, description="User message")


class SessionDraftReplyRequest(BaseModel):
    """Request a suggested next user message without sending it."""

    max_chars: int = Field(1200, ge=100, le=4000, description="Maximum draft length")


class RemoteSessionLaunchRequest(BaseModel):
    """User-initiated remote session launch request."""

    device_id: str = Field(..., min_length=1, description="Target enrolled device id")
    provider: str = Field(..., description="Provider CLI to launch (v1: codex only)")
    cwd: str = Field(..., min_length=1, description="Absolute working directory on the target machine")
    git_repo: str | None = Field(None, description="Optional git repository path")
    git_branch: str | None = Field(None, description="Optional git branch name")
    project: str | None = Field(None, description="Optional project label")
    display_name: str | None = Field(None, description="Optional display name")
    client_request_id: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Optional idempotency key; repeated calls with the same value return the same session",
    )


class RemoteSessionLaunchResponse(BaseModel):
    """Response from POST /api/sessions/launch."""

    session_id: str
    launch_state: str
    launch_error_code: str | None = None
    launch_error_message: str | None = None


class ManagedLocalThisDeviceLaunchRequest(BaseModel):
    """Request to start a managed local AI agent session on the calling device."""

    cwd: str = Field(..., min_length=1, description="Working directory on this device")
    provider: str = Field(
        "claude",
        description="AI provider CLI to launch (claude, codex, opencode, or antigravity)",
    )
    project: str | None = Field(None, description="Optional project label")
    git_repo: str | None = Field(None, description="Optional git repository path")
    git_branch: str | None = Field(None, description="Optional git branch name")
    display_name: str | None = Field(None, description="Optional display name for the session")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.ASSIST, description="assist | autopilot")
    machine_name: str | None = Field(
        None,
        description="Optional local Longhouse machine label override stored on the launched session",
    )
    native_claude_channels_available: bool | None = Field(
        None,
        description="Optional CLI capability hint for whether native Claude channels are available on this device",
    )
    claude_launch_env: dict[str, str] | None = Field(
        None,
        description="Optional allowlisted Claude launch env overrides to apply on the local runner",
    )


class SessionChatError(BaseModel):
    """Error response for session chat."""

    error: str
    code: str
    lock_info: SessionLockInfo | None = None


class SessionInputRequest(BaseModel):
    """User input targeted at a managed session."""

    text: str = Field(..., min_length=1, max_length=10000)
    intent: str = Field(INPUT_INTENT_AUTO, description="auto | queue | steer")
    client_request_id: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Optional client idempotency key for this submitted input",
    )


class QueuedInputSummary(BaseModel):
    id: int
    text: str
    intent: str
    status: str
    last_error: str | None = None
    created_at: datetime | None = None


class SessionInputResponse(BaseModel):
    """Shape returned from POST /api/sessions/{id}/input."""

    outcome: str = Field(..., description="sent | queued")
    input_id: int
    client_request_id: str | None = None
    intent: str
    queued: list[QueuedInputSummary] = Field(default_factory=list)


class SessionInterruptResponse(BaseModel):
    interrupt_dispatched: bool
    confirmed_stopped: bool = False
    session_id: str
    exit_code: int | None = None
    error: str | None = None
    released_lock: bool = False


async def _interrupt_live_session_response(
    *,
    db: Session,
    owner_id: int,
    source_session,
    request_id: str,
) -> SessionInterruptResponse:
    """Dispatch managed-local interrupt through the single control service."""
    from zerg.services.managed_local_control import interrupt_managed_local_session

    lock_scope_id = str(source_session.thread_root_session_id or source_session.id)

    try:
        result = await interrupt_managed_local_session(
            db=db,
            owner_id=owner_id,
            session=source_session,
            commis_id=request_id,
        )
    except Exception as exc:
        released_lock = await session_lock_manager.release(lock_scope_id)
        if released_lock:
            logger.warning(
                "[%s] Released managed-local session lock after interrupt dispatch error for %s",
                request_id,
                source_session.id,
            )
        logger.exception("[%s] Error dispatching managed-local interrupt for %s", request_id, source_session.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "interrupt_dispatch_error",
                "message": f"Internal error: {str(exc)[:200]}",
                "released_lock": released_lock,
                "confirmed_stopped": False,
            },
        ) from exc

    released_lock = await session_lock_manager.release(lock_scope_id)
    if released_lock:
        logger.warning(
            "[%s] Released managed-local session lock during interrupt for %s",
            request_id,
            source_session.id,
        )
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "interrupt_failed",
                "message": str(result.error or "Managed local interrupt failed"),
                "exit_code": result.exit_code,
                "released_lock": released_lock,
                "confirmed_stopped": False,
            },
        )

    return SessionInterruptResponse(
        interrupt_dispatched=True,
        confirmed_stopped=False,
        session_id=str(source_session.id),
        exit_code=result.exit_code,
        released_lock=released_lock,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{session_id}/send-live")
async def send_to_live_session(
    session_id: str,
    body: SessionMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
):
    """Send text into the live managed-local session and return a fast JSON ack."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    logger.info(f"[{request_id}] Live session send request for session {source_session.id}")
    _assert_live_session_send_available(db, source_session, owner_id=current_user.id)
    lock_scope_id = await _acquire_session_lock_or_raise(source_session=source_session, request_id=request_id)
    try:
        return await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=current_user.id,
            message=body.message,
            request_id=request_id,
            lock_scope_id=lock_scope_id,
            db=db,
        )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, request_id)
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, request_id)
        logger.exception(f"[{request_id}] Error in send_to_live_session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc


@router.post("/{session_id}/draft-reply", response_model=SessionDraftReplyResponse)
async def draft_reply_for_live_session(
    session_id: str,
    body: SessionDraftReplyRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
):
    """Generate a suggested next user message for a live managed-local session."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _assert_live_session_send_available(db, source_session, owner_id=current_user.id)
    try:
        max_chars = (body or SessionDraftReplyRequest()).max_chars
        return await _build_managed_local_draft_reply_response(
            source_session=source_session,
            request_id=request_id,
            max_chars=max_chars,
            db=db,
            owner_id=current_user.id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[%s] Error in draft_reply_for_live_session", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc


@agents_router.post("/{session_id}/send-live")
async def send_to_live_session_agents(
    session_id: str,
    body: SessionMessageRequest,
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
):
    """Machine-facing explicit live-send surface for managed-local sessions."""
    settings = get_settings()
    resolved_device_token = device_token if isinstance(device_token, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    _assert_live_session_send_available(db, source_session, owner_id=owner_id)
    lock_scope_id = await _acquire_session_lock_or_raise(source_session=source_session, request_id=request_id)

    try:
        return await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=owner_id,
            message=body.message,
            request_id=request_id,
            lock_scope_id=lock_scope_id,
            db=db,
        )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, request_id)
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, request_id)
        logger.exception(f"[{request_id}] Error in send_to_live_session_agents")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc


@agents_router.post("/{session_id}/draft-reply", response_model=SessionDraftReplyResponse)
async def draft_reply_for_live_session_agents(
    session_id: str,
    request: Request,
    body: SessionDraftReplyRequest | None = None,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
):
    """Machine-facing draft-reply surface for managed-local sessions."""
    settings = get_settings()
    resolved_device_token = device_token if isinstance(device_token, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    _assert_live_session_send_available(db, source_session, owner_id=owner_id)

    try:
        max_chars = (body or SessionDraftReplyRequest()).max_chars
        return await _build_managed_local_draft_reply_response(
            source_session=source_session,
            request_id=request_id,
            max_chars=max_chars,
            db=db,
            owner_id=owner_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[%s] Error in draft_reply_for_live_session_agents", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc


@router.post("/{session_id}/interrupt-live", response_model=SessionInterruptResponse)
async def interrupt_live_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> SessionInterruptResponse:
    """Browser-authenticated explicit interrupt for managed-local sessions."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _assert_live_session_send_available(db, source_session, owner_id=current_user.id)
    return await _interrupt_live_session_response(
        db=db,
        owner_id=current_user.id,
        source_session=source_session,
        request_id=request_id,
    )


@agents_router.post("/{session_id}/interrupt-live", response_model=SessionInterruptResponse)
async def interrupt_live_session_agents(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionInterruptResponse:
    """Machine-facing explicit interrupt for managed-local sessions.

    A successful response means the interrupt command was dispatched on the
    source runner. It does not confirm that the provider stopped the turn.
    """
    settings = get_settings()
    resolved_device_token = device_token if isinstance(device_token, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    _assert_live_session_send_available(db, source_session, owner_id=owner_id)
    return await _interrupt_live_session_response(
        db=db,
        owner_id=owner_id,
        source_session=source_session,
        request_id=request_id,
    )


@router.post("/managed-local/this-device", response_model=ManagedLocalSessionLaunchResponse)
async def launch_managed_local_this_device(
    body: ManagedLocalThisDeviceLaunchRequest,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
):
    """Start a managed local AI agent session on the calling machine's connected runner."""

    settings = get_settings()
    owner_id = _resolve_agents_owner_id(db, device_token)
    token_device_id = str(getattr(device_token, "device_id", "") or "").strip()
    machine_name = (body.machine_name or "").strip() or token_device_id
    if not token_device_id and settings.auth_disabled:
        token_device_id = machine_name
    if not token_device_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Device token is missing device_id")
    # machine_name is a display label; routing is always by device_id.
    runner_target = token_device_id

    try:
        params = ManagedLocalLaunchParams(
            owner_id=owner_id,
            runner_target=runner_target,
            cwd=body.cwd,
            provider=body.provider,
            project=body.project,
            git_repo=body.git_repo,
            git_branch=body.git_branch,
            display_name=body.display_name,
            loop_mode=coerce_session_loop_mode(body.loop_mode).value,
            machine_name=machine_name,
            native_claude_channels_available=body.native_claude_channels_available,
            claude_launch_env=body.claude_launch_env,
        )
        ws = get_write_serializer()
        result = await ws.execute_or_direct(
            lambda write_db: launch_managed_local_session_sync(write_db, params),
            db,
            label="managed-launch",
        )
    except ManagedLocalLaunchError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception:
        db.rollback()
        logger.exception("Managed local launch for this device failed unexpectedly")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Managed local launch failed",
        )

    from zerg.services.session_pubsub import publish_session_runtime_update

    publish_session_runtime_update(
        session_id=str(result.session.id),
        provider=str(result.session.provider or body.provider or ""),
        source="managed_local_launch",
    )

    return _managed_local_launch_response(db, result)


@router.post("/launch", response_model=RemoteSessionLaunchResponse)
async def launch_remote_session_endpoint(
    body: RemoteSessionLaunchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> RemoteSessionLaunchResponse:
    """Start a session on a user-owned machine via the Machine Agent control channel.

    See docs/specs/remote-session-launch.md. Pre-allocates a session UUID,
    inserts the ``sessions`` row in ``launch_state=launching``, and dispatches
    ``session.launch`` over the existing control WebSocket.
    """
    try:
        result = await launch_remote_session(
            db,
            RemoteLaunchParams(
                owner_id=int(current_user.id),
                device_id=body.device_id,
                provider=body.provider,
                cwd=body.cwd,
                git_repo=body.git_repo,
                git_branch=body.git_branch,
                project=body.project,
                display_name=body.display_name,
                client_request_id=body.client_request_id,
            ),
        )
    except RemoteLaunchError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail}) from exc
    except Exception:
        db.rollback()
        logger.exception("Remote session launch failed unexpectedly")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Remote session launch failed")

    return RemoteSessionLaunchResponse(
        session_id=str(result.session_id),
        launch_state=result.launch_state,
        launch_error_code=result.launch_error_code,
        launch_error_message=result.launch_error_message,
    )


@router.get("/{session_id}/lock")
async def get_session_lock_status(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_browser_route_user),
) -> SessionLockInfo:
    """Check if a session is currently locked.

    Used by UI to show lock status before attempting to chat.
    """
    lock_scope_id = _lock_scope_id_for_session(db, session_id)
    lock = await session_lock_manager.get_lock_info(lock_scope_id)

    if lock:
        return SessionLockInfo(
            locked=True,
            holder=lock.holder,
            time_remaining_seconds=lock.time_remaining,
            fork_available=True,
        )
    else:
        return SessionLockInfo(
            locked=False,
            fork_available=False,
        )


def _queued_summary(row) -> QueuedInputSummary:
    return QueuedInputSummary(
        id=int(row.id),
        text=row.body,
        intent=row.intent,
        status=row.status,
        last_error=row.last_error,
        created_at=row.created_at,
    )


def _client_request_id_for_input(body: SessionInputRequest) -> str:
    client_request_id = (body.client_request_id or "").strip()
    if client_request_id:
        return client_request_id
    return uuid.uuid4().hex


def _input_conflict(existing: SessionInput, *, reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": "input_conflict",
            "existing_input_id": int(existing.id),
            "reason": reason,
        },
    )


def _conflict_for_existing_input(existing: SessionInput) -> HTTPException:
    status_value = str(existing.status or "")
    if status_value == INPUT_STATUS_CANCELLED:
        return _input_conflict(existing, reason="cancelled")
    if status_value == INPUT_STATUS_FAILED:
        last_error = str(existing.last_error or "").strip()
        if last_error == "turn_ended":
            return HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "turn_ended",
                    "message": "The active turn already ended. Queue this as the next message instead?",
                },
            )
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "input_failed",
                "message": last_error or "This submitted input already failed. Edit and send it again.",
            },
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": "input_conflict",
            "message": "This submitted input is not retryable. Edit and send it again.",
        },
    )


def _find_existing_input(
    *,
    source_session,
    owner_id: int,
    client_request_id: str,
    db: Session,
) -> SessionInput | None:
    if not client_request_id:
        return None
    return (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == source_session.id,
            SessionInput.owner_id == owner_id,
            SessionInput.client_request_id == client_request_id,
        )
        .order_by(SessionInput.id.asc())
        .first()
    )


def _existing_input_response(
    *,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    db: Session,
) -> SessionInputResponse | None:
    client_request_id = (body.client_request_id or "").strip()
    if not client_request_id:
        return None
    existing = _find_existing_input(
        source_session=source_session,
        owner_id=owner_id,
        client_request_id=client_request_id,
        db=db,
    )
    if existing is None:
        return None
    if existing.body != body.text:
        raise _input_conflict(existing, reason="different_text")
    if existing.status not in (INPUT_STATUS_DELIVERED, INPUT_STATUS_QUEUED, INPUT_STATUS_DELIVERING):
        raise _conflict_for_existing_input(existing)
    recent = list_recent_inputs(db, source_session.id)
    outcome = "sent" if existing.status == INPUT_STATUS_DELIVERED else "queued"
    return SessionInputResponse(
        outcome=outcome,
        input_id=int(existing.id),
        client_request_id=existing.client_request_id,
        intent=existing.intent,
        queued=[_queued_summary(r) for r in recent],
    )


def _create_session_input_or_existing(
    *,
    db: Session,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    intent: str,
    status_value: str,
    client_request_id: str,
    delivery_request_id: str | None = None,
) -> SessionInput | SessionInputResponse:
    try:
        return create_session_input(
            db,
            session_id=source_session.id,
            text=body.text,
            owner_id=owner_id,
            intent=intent,
            status=status_value,
            client_request_id=client_request_id,
            delivery_request_id=delivery_request_id,
        )
    except IntegrityError:
        db.rollback()
        existing = _find_existing_input(
            source_session=source_session,
            owner_id=owner_id,
            client_request_id=client_request_id,
            db=db,
        )
        if existing is not None:
            if existing.body != body.text:
                raise _input_conflict(existing, reason="different_text")
            if existing.status == INPUT_STATUS_FAILED:
                row = retry_failed_input(
                    db,
                    int(existing.id),
                    intent=intent,
                    status=status_value,
                    delivery_request_id=delivery_request_id,
                )
                if row is None:
                    raise _conflict_for_existing_input(existing)
                return row
        if existing_response := _existing_input_response(
            source_session=source_session,
            owner_id=owner_id,
            body=body,
            db=db,
        ):
            return existing_response
        raise


async def _dispatch_steer_input(
    *,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    client_request_id: str,
    delivery_request_id: str,
    db: Session,
    retry_row: SessionInput | None = None,
) -> SessionInputResponse:
    """Send a mid-turn steer, surfacing turn-ended races as a structured 409.

    Codex's own guidance: do not silently fall back to queue when the user
    chose intent=steer — the intent is corrective, and a silent queue could
    cause the message to land later than desired without the user noticing.
    """
    from zerg.services.managed_local_control import MANAGED_LOCAL_STEER_TURN_ENDED
    from zerg.services.managed_local_control import steer_text_to_managed_local_session

    capabilities = current_session_capabilities(db, source_session, owner_id=owner_id)
    if not capabilities.can_steer_active_turn:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "steer_unsupported",
                "message": "This session does not support mid-turn steer on the current transport.",
            },
        )

    # Record a delivering row so the steer attempt is audit-visible even on
    # failure (for drain-failure UX parity with intent=auto).
    if retry_row is not None:
        row = retry_failed_input(
            db,
            int(retry_row.id),
            intent=INPUT_INTENT_STEER,
            status=INPUT_STATUS_DELIVERING,
            delivery_request_id=delivery_request_id,
        )
        if row is None:
            raise _conflict_for_existing_input(retry_row)
    else:
        created = _create_session_input_or_existing(
            db=db,
            source_session=source_session,
            owner_id=owner_id,
            body=body,
            intent=INPUT_INTENT_STEER,
            status_value=INPUT_STATUS_DELIVERING,
            client_request_id=client_request_id,
            delivery_request_id=delivery_request_id,
        )
        if isinstance(created, SessionInputResponse):
            return created
        row = created

    result = await steer_text_to_managed_local_session(
        db=db,
        owner_id=owner_id,
        session=source_session,
        text=body.text,
        commis_id=delivery_request_id,
    )

    if result.ok:
        from zerg.services.session_inputs import mark_delivered as _mark_input_delivered

        _mark_input_delivered(db, int(row.id))
        recent = list_recent_inputs(db, source_session.id)
        return SessionInputResponse(
            outcome="sent",
            input_id=int(row.id),
            client_request_id=row.client_request_id,
            intent=INPUT_INTENT_STEER,
            queued=[_queued_summary(r) for r in recent],
        )

    # Turn-ended race: explicit code so the UI can prompt the user to
    # queue-next instead of silently deciding for them.
    if result.error == MANAGED_LOCAL_STEER_TURN_ENDED:
        _mark_input_failed(db, int(row.id), error="turn_ended")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "turn_ended",
                "message": "The active turn already ended. Queue this as the next message instead?",
            },
        )

    # Generic failure — still mark failed and bubble up.
    _mark_input_failed(db, int(row.id), error=str(result.error or "steer failed")[:200])
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "error_code": "steer_failed",
            "message": str(result.error or "Managed local steer failed"),
        },
    )


async def _create_session_input_response(
    *,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    db: Session,
) -> SessionInputResponse:
    if body.intent not in (INPUT_INTENT_AUTO, INPUT_INTENT_QUEUE, INPUT_INTENT_STEER):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown intent: {body.intent}",
        )

    _assert_live_session_send_available(db, source_session, owner_id=owner_id)

    client_request_id = _client_request_id_for_input(body)
    delivery_request_id = uuid.uuid4().hex
    existing_input = _find_existing_input(
        source_session=source_session,
        owner_id=owner_id,
        client_request_id=client_request_id,
        db=db,
    )
    if existing_input is not None:
        if existing_input.body != body.text:
            raise _input_conflict(existing_input, reason="different_text")
        if existing_input.status == INPUT_STATUS_CANCELLED:
            raise _input_conflict(existing_input, reason="cancelled")
        if existing_input.status != INPUT_STATUS_FAILED:
            if existing_response := _existing_input_response(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                db=db,
            ):
                return existing_response

    def _cap_check_or_raise() -> None:
        current = count_queued(db, source_session.id)
        if current >= MAX_QUEUED_PER_SESSION:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(f"Too many queued inputs for this session ({current}); " "cancel one before queuing another"),
            )

    if body.intent == INPUT_INTENT_STEER:
        return await _dispatch_steer_input(
            source_session=source_session,
            owner_id=owner_id,
            body=body,
            client_request_id=client_request_id,
            delivery_request_id=delivery_request_id,
            db=db,
            retry_row=existing_input,
        )

    # Queue intent always persists and returns without attempting dispatch.
    if body.intent == INPUT_INTENT_QUEUE:
        _cap_check_or_raise()
        if existing_input is not None:
            row = retry_failed_input(
                db,
                int(existing_input.id),
                intent=INPUT_INTENT_QUEUE,
                status=INPUT_STATUS_QUEUED,
                delivery_request_id=None,
            )
            if row is None:
                raise _conflict_for_existing_input(existing_input)
        else:
            created = _create_session_input_or_existing(
                db=db,
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                intent=INPUT_INTENT_QUEUE,
                status_value=INPUT_STATUS_QUEUED,
                client_request_id=client_request_id,
            )
            if isinstance(created, SessionInputResponse):
                return created
            row = created
        recent = list_recent_inputs(db, source_session.id)
        return SessionInputResponse(
            outcome="queued",
            input_id=int(row.id),
            client_request_id=row.client_request_id,
            intent=INPUT_INTENT_QUEUE,
            queued=[_queued_summary(r) for r in recent],
        )

    # Auto: try to send now; if the session is locked, persist as queued.
    lock_scope_id = str(source_session.thread_root_session_id or source_session.id)
    lock = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder=delivery_request_id,
        ttl_seconds=300,
    )
    if not lock:
        _cap_check_or_raise()
        if existing_input is not None:
            row = retry_failed_input(
                db,
                int(existing_input.id),
                intent=INPUT_INTENT_AUTO,
                status=INPUT_STATUS_QUEUED,
                delivery_request_id=None,
            )
            if row is None:
                raise _conflict_for_existing_input(existing_input)
        else:
            created = _create_session_input_or_existing(
                db=db,
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_QUEUED,
                client_request_id=client_request_id,
            )
            if isinstance(created, SessionInputResponse):
                return created
            row = created
        recent = list_recent_inputs(db, source_session.id)
        return SessionInputResponse(
            outcome="queued",
            input_id=int(row.id),
            client_request_id=row.client_request_id,
            intent=INPUT_INTENT_AUTO,
            queued=[_queued_summary(r) for r in recent],
        )

    # Lock acquired: record a delivering row for audit, then dispatch.
    try:
        if existing_input is not None:
            row = retry_failed_input(
                db,
                int(existing_input.id),
                intent=INPUT_INTENT_AUTO,
                status=INPUT_STATUS_DELIVERING,
                delivery_request_id=delivery_request_id,
            )
            if row is None:
                raise _conflict_for_existing_input(existing_input)
            created: SessionInput | SessionInputResponse = row
        else:
            created = _create_session_input_or_existing(
                db=db,
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_DELIVERING,
                client_request_id=client_request_id,
                delivery_request_id=delivery_request_id,
            )
    except Exception:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        raise
    if isinstance(created, SessionInputResponse):
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        return created
    row = created
    try:
        dispatch_response = await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=owner_id,
            message=body.text,
            request_id=delivery_request_id,
            lock_scope_id=lock_scope_id,
            db=db,
            session_input_id=int(row.id),
        )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        _mark_input_failed(db, int(row.id), error="dispatch rejected")
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        _mark_input_failed(db, int(row.id), error=str(exc)[:200])
        logger.exception(f"[{delivery_request_id}] Error dispatching session input")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc

    # _build_managed_local_chat_response may return a 502 JSONResponse on send
    # failure (it releases the lock itself). Propagate as HTTPException so the
    # input row is marked failed and the client sees the error.
    dispatch_status = int(getattr(dispatch_response, "status_code", 200) or 200)
    if dispatch_status >= 400:
        response_error_code = "send_failed"
        response_error_message = f"Managed local dispatch returned {dispatch_status}"
        try:
            response_body = json.loads(getattr(dispatch_response, "body", b"{}") or b"{}")
            if isinstance(response_body, dict):
                response_error_code = str(response_body.get("error_code") or response_error_code)
                response_error_message = str(response_body.get("error") or response_error_message)
        except Exception:
            pass
        _mark_input_failed(db, int(row.id), error=response_error_message[:200])
        # Lock already released by _dispatch_managed_local_text on failure.
        raise HTTPException(
            status_code=dispatch_status,
            detail={
                "error_code": response_error_code,
                "message": response_error_message,
            },
        )

    # Dispatch accepted — mark the row delivered. Lock release + terminal-phase
    # observation is handled inside the existing dispatch path.
    from zerg.services.session_inputs import mark_delivered as _mark_input_delivered

    _mark_input_delivered(db, int(row.id))
    recent = list_recent_inputs(db, source_session.id)
    return SessionInputResponse(
        outcome="sent",
        input_id=int(row.id),
        client_request_id=row.client_request_id,
        intent=INPUT_INTENT_AUTO,
        queued=[_queued_summary(r) for r in recent],
    )


@router.post("/{session_id}/input", response_model=SessionInputResponse)
async def create_session_input_endpoint(
    session_id: str,
    body: SessionInputRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> SessionInputResponse:
    source_session = _load_session_for_continuation(db, session_id)
    return await _create_session_input_response(
        source_session=source_session,
        owner_id=current_user.id,
        body=body,
        db=db,
    )


@router.get("/{session_id}/inputs")
async def list_session_inputs_endpoint(
    session_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_browser_route_user),
):
    """List queued + recently-failed inputs for the chip UI.

    The web composer polls this every 2s while any row is queued or
    delivering. Most polls return the same shape, so we emit a weak
    ETag derived from the row state tuple and honor If-None-Match →
    304. A 304 is ~1ms vs ~9ms for the full response, which matters
    at the aggregate QPS of many active session-detail pages.
    """
    source_session = _load_session_for_continuation(db, session_id)
    rows = list_recent_inputs(db, source_session.id)

    # Cheap stable hash of the state that matters to the client. If none of
    # id/status/updated_at/last_error changed, neither did the chip.
    hasher = blake2b(digest_size=12)
    for r in rows:
        hasher.update(f"{r.id}:{r.status}:{r.updated_at}:{r.last_error or ''}|".encode())
    etag = f'W/"inputs-{hasher.hexdigest()}"'

    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    return [_queued_summary(r) for r in rows]


@router.delete("/{session_id}/inputs/{input_id}")
async def cancel_session_input_endpoint(
    session_id: str,
    input_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_browser_route_user),
) -> dict:
    source_session = _load_session_for_continuation(db, session_id)
    existing = get_session_input(db, input_id)
    if existing is None or existing.session_id != source_session.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="queued input not found for this session",
        )
    row = cancel_queued_input(db, input_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="input is no longer queued",
        )
    return {"cancelled": True, "input_id": int(row.id)}


@router.delete("/{session_id}/lock")
async def force_release_lock(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_browser_route_user),
) -> dict:
    """Force release a session lock (admin operation).

    Use with caution - may cause issues if a chat is in progress.
    """
    lock_scope_id = _lock_scope_id_for_session(db, session_id)
    released = await session_lock_manager.release(lock_scope_id)
    return {
        "released": released,
        "session_id": session_id,
        "lock_session_id": lock_scope_id,
    }
