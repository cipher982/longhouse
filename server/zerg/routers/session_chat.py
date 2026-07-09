"""Session control router for managed-local live-send and launch.

Enables live interaction with managed-local CLI sessions launched through
Longhouse. Per-session locks prevent concurrent send collisions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from hashlib import blake2b
from typing import Any

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

import zerg.database as database_module
from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import SessionInput
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.services.live_archive_outbox import enqueue_managed_local_launch_outbox
from zerg.services.live_archive_outbox import project_session_input_receipt_to_archive
from zerg.services.live_launch_readiness import upsert_live_launch_readiness
from zerg.services.live_session_inputs import LiveInputReceiptSnapshot
from zerg.services.live_session_inputs import cancel_live_queued_receipt
from zerg.services.live_session_inputs import count_live_queued_receipts
from zerg.services.live_session_inputs import list_recent_live_input_receipts
from zerg.services.live_session_inputs import load_live_input_receipt_by_client_request_best_effort
from zerg.services.live_session_inputs import record_live_input_receipt_best_effort
from zerg.services.managed_local_control import answer_pause_request_on_managed_local_session
from zerg.services.managed_local_launcher import ManagedLocalLaunchError
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan
from zerg.services.managed_local_launcher import resolve_managed_local_launch_runner
from zerg.services.remote_session_launch import RemoteContinueParams
from zerg.services.remote_session_launch import RemoteLaunchError
from zerg.services.remote_session_launch import RemoteLaunchParams
from zerg.services.remote_session_launch import continue_remote_session
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
from zerg.services.session_chat_impl import _managed_local_launch_response_from_plan
from zerg.services.session_chat_impl import _resolve_agents_owner_id
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
from zerg.services.session_inputs import InputConflictReason
from zerg.services.session_inputs import InputIntent
from zerg.services.session_inputs import InputOutcome
from zerg.services.session_inputs import InputStatus
from zerg.services.session_inputs import cancel_queued_input
from zerg.services.session_inputs import count_queued
from zerg.services.session_inputs import create_session_input
from zerg.services.session_inputs import get_session_input
from zerg.services.session_inputs import list_recent_inputs
from zerg.services.session_inputs import mark_failed as _mark_input_failed
from zerg.services.session_inputs import retry_failed_input
from zerg.services.session_kernel_projection import session_lock_scope_id
from zerg.services.session_launch_lifecycle import DEFAULT_REMOTE_CONTINUE_MESSAGE_LIFETIME
from zerg.services.session_launch_lifecycle import DEFAULT_REMOTE_SESSION_LAUNCH_LIFETIME
from zerg.services.session_launch_lifecycle import RemoteExecutionLifetime
from zerg.services.session_launch_lifecycle import RemoteLaunchErrorCode
from zerg.services.session_launch_lifecycle import RemoteLaunchLifecycleState
from zerg.services.session_launch_provenance import LAUNCH_ACTOR_HUMAN_UI
from zerg.services.session_launch_provenance import LAUNCH_SURFACE_API
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_pause_requests import PENDING_STATUS as PAUSE_PENDING_STATUS
from zerg.services.session_pause_requests import get_pause_request_for_session
from zerg.services.session_pause_requests import is_pull_reply_transport
from zerg.services.session_pause_requests import list_pause_requests_for_session
from zerg.services.session_pause_requests import load_active_pause_request_for_session
from zerg.services.session_pause_requests import resolve_pause_request
from zerg.services.session_pause_requests import serialize_pause_request_projection
from zerg.services.session_runtime import current_presence_state_for_session
from zerg.services.session_views import SessionPauseRequestProjectionResponse
from zerg.services.write_serializer import get_live_write_serializer
from zerg.session_loop_mode import SessionLoopMode
from zerg.session_loop_mode import coerce_session_loop_mode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])
_STEER_ACTIVE_PRESENCE_STATES = frozenset({"thinking", "running"})
_MANAGED_LOCAL_HOT_LAUNCH_LEASE_SECS = 300


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
    initial_prompt: str | None = Field(None, min_length=1, max_length=20000, description="Initial one-shot prompt")
    execution_lifetime: RemoteExecutionLifetime | None = Field(
        None,
        description="Remote launch execution lifetime: one_shot|live_control. Omitted defaults to one_shot.",
    )
    client_request_id: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Optional idempotency key; repeated calls with the same value return the same session",
    )


class RemoteSessionLaunchResponse(BaseModel):
    """Response from POST /api/sessions/launch."""

    session_id: str
    launch_state: RemoteLaunchLifecycleState
    execution_lifetime: RemoteExecutionLifetime
    launch_error_code: RemoteLaunchErrorCode | None = None
    launch_error_message: str | None = None


async def _launch_managed_local_session_serialized(
    db: Session,
    params: ManagedLocalLaunchParams,
) -> tuple[Any, ManagedLocalSessionLaunchResponse]:
    runner = resolve_managed_local_launch_runner(db, params)
    plan = build_managed_local_launch_plan(params, runner=runner)
    launch_response = _managed_local_launch_response_from_plan(plan, owner_id=params.owner_id)
    await _write_hot_managed_local_launch_readiness(
        plan,
        owner_id=params.owner_id,
        git_repo=params.git_repo,
        git_branch=params.git_branch,
    )
    return None, launch_response


async def _write_hot_managed_local_launch_readiness(
    plan,
    *,
    owner_id: int,
    git_repo: str | None,
    git_branch: str | None,
) -> None:
    if not database_module.live_store_configured():
        raise ManagedLocalLaunchError(
            "Managed local launch is blocked because Live Store is unavailable; retry shortly.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    live_ws = get_live_write_serializer()
    if not live_ws.is_configured:
        raise ManagedLocalLaunchError(
            "Managed local launch is blocked because Live Store writer is unavailable; retry shortly.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_MANAGED_LOCAL_HOT_LAUNCH_LEASE_SECS)

    def _write(live_db: Session):
        readiness = upsert_live_launch_readiness(
            live_db,
            session_id=plan.session_id,
            owner_id=owner_id,
            device_id=plan.source_name,
            provider=plan.provider,
            execution_lifetime="live_control",
            state="pending",
            command_id=f"managed-local-{plan.session_id}",
            client_request_id=None,
            machine_id=plan.source_name,
            project=plan.project,
            expires_at=expires_at,
            now=now,
        )
        enqueue_managed_local_launch_outbox(
            live_db,
            plan=plan,
            owner_id=owner_id,
            git_repo=git_repo,
            git_branch=git_branch,
            started_at=now,
        )
        return readiness

    try:
        await live_ws.execute(_write, label="managed-launch-readiness")
    except Exception as exc:
        logger.exception("Managed local hot launch readiness write failed")
        raise ManagedLocalLaunchError(
            "Managed local launch is blocked because Live Store writer failed; retry shortly.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


class RemoteSessionContinueRequest(BaseModel):
    """User-initiated request to continue an existing durable session."""

    device_id: str | None = Field(
        None,
        min_length=1,
        description="Target enrolled device id; defaults to the session host",
    )
    cwd: str | None = Field(None, min_length=1, description="Absolute working directory; defaults to the session cwd")
    message: str | None = Field(
        None,
        min_length=1,
        max_length=20000,
        description="Optional follow-up prompt for bounded one-shot continuation",
    )
    execution_lifetime: RemoteExecutionLifetime | None = Field(
        None,
        description="Continuation execution lifetime: one_shot|live_control. Message-bearing browser/iOS requests default to one_shot.",
    )
    client_request_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Required idempotency key; repeated calls with the same value return the same attempt",
    )


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
    permission_mode: str = Field(
        "bypass",
        description="Managed permission policy: 'bypass' (autonomous, default) or 'remote_approve' (answer permission prompts via Longhouse)",
    )
    launch_actor: str | None = Field(None, description="Positive launch actor provenance when known")
    launch_surface: str | None = Field(None, description="Launch surface provenance when known")


class SessionChatError(BaseModel):
    """Error response for session chat."""

    error: str
    code: str
    lock_info: SessionLockInfo | None = None


class SessionInputRequest(BaseModel):
    """User input targeted at a managed session."""

    text: str = Field(..., min_length=1, max_length=10000)
    intent: InputIntent = Field(INPUT_INTENT_AUTO, description="auto | queue | steer")
    client_request_id: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Optional client idempotency key for this submitted input",
    )


class QueuedInputSummary(BaseModel):
    id: int | None = None
    live_input_id: str | None = None
    text: str
    intent: InputIntent
    status: InputStatus
    last_error: str | None = None
    created_at: datetime | None = None


class SessionInputResponse(BaseModel):
    """Shape returned from POST /api/sessions/{id}/input."""

    outcome: InputOutcome = Field(..., description="sent | queued")
    input_id: int | None = None
    live_input_id: str | None = None
    client_request_id: str | None = None
    intent: InputIntent
    queued: list[QueuedInputSummary] = Field(default_factory=list)


class PauseRequestListResponse(BaseModel):
    requests: list[SessionPauseRequestProjectionResponse]
    total: int


class PauseRequestResponseRequest(BaseModel):
    decision: str = Field("answer", description="answer | reject | cancel")
    answers: dict[str, Any] | None = None
    content: Any | None = None
    message: str | None = Field(None, max_length=4000)


class PauseRequestResponseResponse(BaseModel):
    status: str
    pause_request: SessionPauseRequestProjectionResponse


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

    lock_scope_id = session_lock_scope_id(source_session.id)

    try:
        result = await interrupt_managed_local_session(
            db=db,
            owner_id=owner_id,
            session=source_session,
            request_id=request_id,
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


class SessionTerminateResponse(BaseModel):
    terminate_dispatched: bool
    session_id: str
    exit_code: int | None = None
    error: str | None = None
    released_lock: bool = False


async def _terminate_live_session_response(
    *,
    db: Session,
    owner_id: int,
    source_session,
    request_id: str,
) -> SessionTerminateResponse:
    """Dispatch managed-local terminate through the single control service."""
    from zerg.services.managed_local_control import terminate_managed_local_session

    lock_scope_id = session_lock_scope_id(source_session.id)

    try:
        result = await terminate_managed_local_session(
            db=db,
            owner_id=owner_id,
            session=source_session,
            request_id=request_id,
        )
    except Exception as exc:
        released_lock = await session_lock_manager.release(lock_scope_id)
        if released_lock:
            logger.warning(
                "[%s] Released managed-local session lock after terminate dispatch error for %s",
                request_id,
                source_session.id,
            )
        logger.exception("[%s] Error dispatching managed-local terminate for %s", request_id, source_session.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "terminate_dispatch_error",
                "message": f"Internal error: {str(exc)[:200]}",
                "released_lock": released_lock,
            },
        ) from exc

    released_lock = await session_lock_manager.release(lock_scope_id)
    if released_lock:
        logger.warning(
            "[%s] Released managed-local session lock during terminate for %s",
            request_id,
            source_session.id,
        )
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "terminate_failed",
                "message": str(result.error or "Managed local terminate failed"),
                "exit_code": result.exit_code,
                "released_lock": released_lock,
            },
        )

    return SessionTerminateResponse(
        terminate_dispatched=True,
        session_id=str(source_session.id),
        exit_code=result.exit_code,
        released_lock=released_lock,
    )


def _pause_request_projection_or_empty(row) -> dict[str, Any]:
    return serialize_pause_request_projection(row) or {}


def _pending_pause_request_conflict(row) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "pause_request_pending",
            "error_code": "pause_request_pending",
            "message": "Answer the pending provider question before sending a new prompt.",
            "pause_request_id": str(row.id),
        },
    )


def _not_answerable_pause_request(row) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "pause_request_not_answerable",
            "error_code": "pause_request_not_answerable",
            "message": "Answer this request in the terminal.",
            "pause_request_id": str(row.id),
        },
    )


def _list_pause_requests_response(
    *,
    source_session,
    status_filter: str | None,
    db: Session,
) -> PauseRequestListResponse:
    rows = list_pause_requests_for_session(
        db,
        source_session.id,
        status=status_filter,
    )
    requests = [_pause_request_projection_or_empty(row) for row in rows]
    return PauseRequestListResponse(requests=requests, total=len(requests))


def _resolve_pull_pause_request_in_place(
    *,
    db: Session,
    row,
    decision: str,
    response_message: str | None,
) -> PauseRequestResponseResponse:
    """Resolve a PULL-transport pause request without a live push.

    The provider polls Longhouse for the resolved row (e.g. the Claude PreToolUse
    permission hook polls GET /agents/permission-decision), so the answer only
    needs to be persisted: we map answer->allow / reject|cancel->deny into
    permissionDecision and resolve the row. The provider reads it and applies the
    decision.
    """
    permission_decision = "allow" if decision == "answer" else "deny"
    status_value = "resolved" if decision == "answer" else "rejected"
    response_payload = {
        "permissionDecision": permission_decision,
        "permissionDecisionReason": response_message or f"Longhouse {permission_decision}",
        "decision": decision,
    }
    resolved = resolve_pause_request(
        db,
        pause_request_id=row.id,
        status=status_value,
        occurred_at=datetime.now(timezone.utc),
        response_payload=response_payload,
        response_text=response_message,
    )
    db.commit()
    if resolved is None:
        db.refresh(row)
        resolved = row
    return PauseRequestResponseResponse(
        status=status_value,
        pause_request=_pause_request_projection_or_empty(resolved),
    )


def _pause_request_projection_with_terminal_status(
    row,
    *,
    status_value: str,
    resolved_at: datetime,
) -> dict[str, Any]:
    projection = _pause_request_projection_or_empty(row)
    projection["status"] = status_value
    projection["resolved_at"] = resolved_at
    return projection


def _resolve_push_pause_request_best_effort(
    *,
    db: Session,
    row,
    status_value: str,
    resolved_at: datetime,
    response_payload: dict[str, Any],
    response_text: str | None,
) -> dict[str, Any]:
    """Resolve a PUSH pause row without holding the user ACK hostage.

    The provider answer has already been delivered over managed control. If the
    archive DB is saturated here, runtime pause-resolution ingest can still
    converge the durable row later, so return the delivered status and log the
    deferred archive update instead of turning a successful answer into a 500.
    """
    fallback_projection = _pause_request_projection_with_terminal_status(
        row,
        status_value=status_value,
        resolved_at=resolved_at,
    )
    try:
        resolved = resolve_pause_request(
            db,
            pause_request_id=row.id,
            status=status_value,
            occurred_at=resolved_at,
            response_payload=response_payload,
            response_text=response_text,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(
            "Pause request archive resolve deferred for %s after push dispatch",
            getattr(row, "id", None),
            exc_info=True,
        )
        return fallback_projection
    if resolved is None:
        db.refresh(row)
        resolved = row
    return _pause_request_projection_or_empty(resolved)


def _opencode_permission_request_id(row) -> str | None:
    """The opencode permission id to reply to, for an opencode_bridge perm row."""
    ref = row.provider_ref_json if isinstance(row.provider_ref_json, dict) else {}
    if str(ref.get("source") or "").strip() != "opencode_bridge":
        return None
    rid = str(ref.get("opencode_request_id") or row.provider_request_id or "").strip()
    return rid or None


async def _resolve_opencode_permission_via_bridge(
    *,
    db: Session,
    row,
    decision: str,
    response_message: str | None,
    request_id: str,
) -> PauseRequestResponseResponse:
    """Deliver an OpenCode permission decision through the bridge, then resolve.

    The runtime host shares the machine with the opencode bridge for managed-local
    sessions, so we invoke opencode_bridge.permission_reply (it resolves bridge
    state from disk and POSTs to the local opencode server's
    /permission/{id}/reply). It does blocking I/O, so run it off the event loop.
    answer->allow, reject|cancel->deny.
    """
    from zerg.cli import opencode_bridge

    bridge_decision = "allow" if decision == "answer" else "deny"

    def _reply() -> None:
        opencode_bridge.permission_reply(
            session_id=str(row.session_id),
            request_id=request_id,
            decision=bridge_decision,
            state_root=None,
            config_dir=None,
            wait_secs=0.0,
        )

    bridge_error: str | None = None
    try:
        await asyncio.to_thread(_reply)
    except SystemExit as exc:  # typer.Exit on bridge failure
        if int(getattr(exc, "code", 0) or 0) != 0:
            bridge_error = f"opencode bridge permission-reply failed (exit {exc.code})"
    except Exception as exc:  # pragma: no cover - defensive
        bridge_error = f"{type(exc).__name__}: {exc}"

    if bridge_error is not None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "opencode_permission_reply_failed",
                "error_code": "opencode_permission_reply_failed",
                "message": bridge_error,
                "pause_request_id": str(row.id),
                "retryable": True,
                "refetch_required": True,
            },
        )

    status_value = "resolved" if decision == "answer" else "rejected"
    resolved_at = datetime.now(timezone.utc)
    pause_projection = _resolve_push_pause_request_best_effort(
        db=db,
        row=row,
        status_value=status_value,
        resolved_at=resolved_at,
        response_payload={"permissionDecision": bridge_decision, "decision": decision, "transport": "opencode_bridge"},
        response_text=response_message,
    )
    return PauseRequestResponseResponse(
        status=status_value,
        pause_request=pause_projection,
    )


async def _respond_to_pause_request(
    *,
    source_session,
    owner_id: int,
    pause_request_id: str,
    body: PauseRequestResponseRequest,
    db: Session,
) -> PauseRequestResponseResponse:
    try:
        parsed_pause_request_id = uuid.UUID(pause_request_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid pause request id: {pause_request_id}",
        ) from exc

    row = get_pause_request_for_session(
        db,
        session_id=source_session.id,
        pause_request_id=parsed_pause_request_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pause request not found for this session",
        )
    if row.status != PAUSE_PENDING_STATUS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "pause_request_not_pending",
                "error_code": "pause_request_not_pending",
                "message": "This provider question has already resolved.",
                "pause_request_id": str(row.id),
            },
        )
    if not row.can_respond:
        raise _not_answerable_pause_request(row)

    decision = str(body.decision or "answer").strip().lower() or "answer"
    if decision not in {"answer", "reject", "cancel"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision must be answer, reject, or cancel",
        )

    response_message = _pause_response_message(row=row, body=body)

    # Dispatch by the row's reply transport, not its kind. PULL transports (the
    # Claude PreToolUse permission hook polls for the decision) have no live
    # process to push to, so we resolve in place and let the provider read it.
    # PUSH transports deliver the answer over managed control.
    if is_pull_reply_transport(row):
        return _resolve_pull_pause_request_in_place(
            db=db,
            row=row,
            decision=decision,
            response_message=response_message,
        )

    # OpenCode permission prompts push the decision to the local opencode server
    # via the bridge (not the engine websocket), then resolve.
    opencode_request_id = _opencode_permission_request_id(row)
    if opencode_request_id is not None:
        return await _resolve_opencode_permission_via_bridge(
            db=db,
            row=row,
            decision=decision,
            response_message=response_message,
            request_id=opencode_request_id,
        )

    result = await answer_pause_request_on_managed_local_session(
        db=db,
        owner_id=owner_id,
        session=source_session,
        request_key=row.request_key,
        decision=decision,
        answers=body.answers,
        content=body.content,
        message=response_message,
        request_id=f"pause-{row.id}",
    )
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "pause_response_dispatch_failed",
                "error_code": "pause_response_dispatch_failed",
                "message": str(result.error or "Failed to dispatch pause response command"),
                "pause_request_id": str(row.id),
                "retryable": True,
                "refetch_required": True,
            },
        )

    bridge_response = dict(result.response_data or {})
    bridge_status = str(bridge_response.get("status") or "").strip().lower()
    status_value = (
        bridge_status if bridge_status in {"resolved", "rejected", "failed"} else ("resolved" if decision == "answer" else "rejected")
    )
    bridge_payload = bridge_response.get("response_payload")
    response_payload: dict[str, Any]
    if isinstance(bridge_payload, dict):
        response_payload = bridge_payload
    else:
        response_payload = {
            "decision": decision,
            "answers": body.answers,
            "content": body.content,
            "message": response_message,
            "dispatch_ok": result.ok,
            "exit_code": result.exit_code,
            "bridge_response": bridge_response or None,
        }
    response_text = str(bridge_response.get("response_text") or response_message or "").strip() or None
    resolved_at = datetime.now(timezone.utc)
    pause_projection = _resolve_push_pause_request_best_effort(
        db=db,
        row=row,
        status_value=status_value,
        resolved_at=resolved_at,
        response_payload=response_payload,
        response_text=response_text,
    )
    return PauseRequestResponseResponse(
        status=status_value,
        pause_request=pause_projection,
    )


def _pause_response_message(*, row, body: PauseRequestResponseRequest) -> str | None:
    explicit = str(body.message or "").strip()
    if explicit:
        return explicit
    if body.content is not None:
        content_text = str(body.content).strip()
        if content_text:
            return content_text
    answers = dict(body.answers or {})
    if not answers:
        return None
    payload = row.request_payload_json if isinstance(row.request_payload_json, dict) else {}
    labels = _pause_question_labels(payload)
    parts: list[str] = []
    for key, raw_value in answers.items():
        answer_key = str(key or "").strip()
        label = labels.get(answer_key) or answer_key
        if isinstance(raw_value, (list, tuple, set)):
            values = [str(item).strip() for item in raw_value if str(item).strip()]
        else:
            values = [str(raw_value).strip()] if str(raw_value).strip() else []
        if label and values:
            parts.append(f"{label}: {', '.join(values)}")
    return "; ".join(parts) if parts else None


def _pause_question_labels(payload: dict[str, Any]) -> dict[str, str]:
    raw_questions = payload.get("questions")
    if raw_questions is None and any(key in payload for key in ("question", "prompt", "options")):
        raw_questions = [payload]
    if not isinstance(raw_questions, list):
        return {}
    labels: dict[str, str] = {}
    for index, item in enumerate(raw_questions):
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("id") or item.get("name") or item.get("key") or f"question_{index + 1}").strip()
        label = str(item.get("header") or item.get("title") or item.get("question") or item.get("prompt") or question_id).strip()
        if question_id and label:
            labels[question_id] = label
    return labels


def _assert_no_answerable_pause_request_pending(*, db: Session, source_session) -> None:
    pause_request = load_active_pause_request_for_session(db, source_session.id)
    if pause_request is not None and pause_request.status == PAUSE_PENDING_STATUS and pause_request.can_respond:
        raise _pending_pause_request_conflict(pause_request)


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


@router.post("/{session_id}/terminate-live", response_model=SessionTerminateResponse)
async def terminate_live_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> SessionTerminateResponse:
    """Browser-authenticated explicit terminate for managed-local sessions."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _assert_live_session_send_available(db, source_session, owner_id=current_user.id)
    return await _terminate_live_session_response(
        db=db,
        owner_id=current_user.id,
        source_session=source_session,
        request_id=request_id,
    )


@agents_router.post("/{session_id}/terminate-live", response_model=SessionTerminateResponse)
async def terminate_live_session_agents(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionTerminateResponse:
    """Machine-facing explicit terminate for managed-local sessions.

    A successful response means the terminate command was dispatched on the
    source runner (the engine signalled the provider child). It is not a
    confirmation that the OS has reaped the process, though most managed
    transports kill the child synchronously.
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
    return await _terminate_live_session_response(
        db=db,
        owner_id=owner_id,
        source_session=source_session,
        request_id=request_id,
    )


@router.get("/{session_id}/pause-requests", response_model=PauseRequestListResponse)
async def list_pause_requests_endpoint(
    session_id: str,
    status_filter: str | None = PAUSE_PENDING_STATUS,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_browser_route_user),
) -> PauseRequestListResponse:
    source_session = _load_session_for_continuation(db, session_id)
    return _list_pause_requests_response(
        source_session=source_session,
        status_filter=status_filter,
        db=db,
    )


@router.post("/{session_id}/pause-requests/{pause_request_id}/response", response_model=PauseRequestResponseResponse)
async def respond_to_pause_request_endpoint(
    session_id: str,
    pause_request_id: str,
    body: PauseRequestResponseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> PauseRequestResponseResponse:
    source_session = _load_session_for_continuation(db, session_id)
    return await _respond_to_pause_request(
        source_session=source_session,
        owner_id=current_user.id,
        pause_request_id=pause_request_id,
        body=body,
        db=db,
    )


@agents_router.get("/{session_id}/pause-requests", response_model=PauseRequestListResponse)
async def list_pause_requests_agents(
    session_id: str,
    request: Request,
    status_filter: str | None = PAUSE_PENDING_STATUS,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> PauseRequestListResponse:
    settings = get_settings()
    resolved_device_token = device_token if isinstance(device_token, DeviceToken) else None
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id)
    return _list_pause_requests_response(
        source_session=source_session,
        status_filter=status_filter,
        db=db,
    )


@agents_router.post("/{session_id}/pause-requests/{pause_request_id}/response", response_model=PauseRequestResponseResponse)
async def respond_to_pause_request_agents(
    session_id: str,
    pause_request_id: str,
    body: PauseRequestResponseRequest,
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> PauseRequestResponseResponse:
    settings = get_settings()
    resolved_device_token = device_token if isinstance(device_token, DeviceToken) else None
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id)
    return await _respond_to_pause_request(
        source_session=source_session,
        owner_id=owner_id,
        pause_request_id=pause_request_id,
        body=body,
        db=db,
    )


@agents_router.post("/{session_id}/continue", response_model=RemoteSessionLaunchResponse)
async def continue_remote_session_agents(
    session_id: uuid.UUID,
    body: RemoteSessionContinueRequest,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> RemoteSessionLaunchResponse:
    """Machine-facing continuation surface for existing durable sessions."""
    owner_id = _resolve_agents_owner_id(db, device_token)
    try:
        result = await continue_remote_session(
            db,
            RemoteContinueParams(
                owner_id=owner_id,
                session_id=session_id,
                device_id=body.device_id,
                cwd=body.cwd,
                client_request_id=body.client_request_id,
                message=body.message,
                execution_lifetime=body.execution_lifetime or "live_control",
            ),
        )
    except RemoteLaunchError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail}) from exc
    except Exception:
        db.rollback()
        logger.exception("Machine-facing remote session continue failed unexpectedly")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Remote session continue failed")

    return RemoteSessionLaunchResponse(
        session_id=str(result.session_id),
        launch_state=result.launch_state,
        execution_lifetime=result.execution_lifetime,
        launch_error_code=result.launch_error_code,
        launch_error_message=result.launch_error_message,
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
            permission_mode=body.permission_mode,
            launch_actor=body.launch_actor,
            launch_surface=body.launch_surface,
        )
        # Managed-local launch is user-facing and hot-path critical. Claim live
        # readiness first; the archive row converges through LiveArchiveOutbox.
        result, launch_response = await _launch_managed_local_session_serialized(db, params)
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

    if result is not None:
        from zerg.services.session_pubsub import publish_session_runtime_update

        publish_session_runtime_update(
            session_id=str(result.session.id),
            provider=str(result.session.provider or body.provider or ""),
            source="managed_local_launch",
        )

    return launch_response


@router.post("/launch", response_model=RemoteSessionLaunchResponse)
async def launch_remote_session_endpoint(
    body: RemoteSessionLaunchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> RemoteSessionLaunchResponse:
    """Start a session on a user-owned machine via the Machine Agent control channel.

    See docs/specs/remote-session-launch.md. Pre-allocates a session UUID,
    records a ``SessionLaunchAttempt(state=pending)``, and dispatches
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
                initial_prompt=body.initial_prompt,
                execution_lifetime=body.execution_lifetime or DEFAULT_REMOTE_SESSION_LAUNCH_LIFETIME,
                client_request_id=body.client_request_id,
                launch_actor=LAUNCH_ACTOR_HUMAN_UI,
                launch_surface=LAUNCH_SURFACE_API,
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
        execution_lifetime=result.execution_lifetime,
        launch_error_code=result.launch_error_code,
        launch_error_message=result.launch_error_message,
    )


@router.post("/{session_id}/continue", response_model=RemoteSessionLaunchResponse)
async def continue_remote_session_endpoint(
    session_id: uuid.UUID,
    body: RemoteSessionContinueRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_browser_route_user),
) -> RemoteSessionLaunchResponse:
    """Continue an existing durable session on a user-owned machine."""
    try:
        result = await continue_remote_session(
            db,
            RemoteContinueParams(
                owner_id=int(current_user.id),
                session_id=session_id,
                device_id=body.device_id,
                cwd=body.cwd,
                client_request_id=body.client_request_id,
                message=body.message,
                execution_lifetime=body.execution_lifetime
                or (DEFAULT_REMOTE_CONTINUE_MESSAGE_LIFETIME if body.message else "live_control"),
            ),
        )
    except RemoteLaunchError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail}) from exc
    except Exception:
        db.rollback()
        logger.exception("Remote session continue failed unexpectedly")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Remote session continue failed")

    return RemoteSessionLaunchResponse(
        session_id=str(result.session_id),
        launch_state=result.launch_state,
        execution_lifetime=result.execution_lifetime,
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


def _live_queued_summary(receipt: LiveInputReceiptSnapshot) -> QueuedInputSummary:
    last_error = None
    if receipt.error_json:
        try:
            payload = json.loads(receipt.error_json)
            if isinstance(payload, dict):
                last_error = str(payload.get("message") or "").strip() or None
        except Exception:
            last_error = receipt.error_json
    return QueuedInputSummary(
        id=receipt.archive_session_input_id,
        live_input_id=receipt.id,
        text=receipt.text,
        intent=receipt.intent if receipt.intent in (INPUT_INTENT_AUTO, INPUT_INTENT_QUEUE, INPUT_INTENT_STEER) else INPUT_INTENT_AUTO,
        status=receipt.status,
        last_error=last_error,
        created_at=receipt.created_at,
    )


def _live_input_store_available() -> bool:
    return bool(database_module.live_store_configured() and database_module.get_live_session_factory() is not None)


def _list_recent_live_input_summaries(source_session) -> list[QueuedInputSummary] | None:
    if not _live_input_store_available():
        return None
    session_factory = database_module.get_live_session_factory()
    if session_factory is None:
        return None
    try:
        with session_factory() as live_db:
            receipts = list_recent_live_input_receipts(live_db, session_id=source_session.id)
        return [_live_queued_summary(receipt) for receipt in receipts]
    except Exception:
        logger.warning("Failed to list live input receipts for session %s", source_session.id, exc_info=True)
        return None


def _recent_input_summaries(source_session, db: Session) -> list[QueuedInputSummary]:
    live_rows = _list_recent_live_input_summaries(source_session)
    if live_rows is not None:
        return live_rows
    return [_queued_summary(row) for row in list_recent_inputs(db, source_session.id)]


def _count_active_live_inputs(source_session) -> int | None:
    if not _live_input_store_available():
        return None
    session_factory = database_module.get_live_session_factory()
    if session_factory is None:
        return None
    try:
        with session_factory() as live_db:
            return count_live_queued_receipts(live_db, session_id=source_session.id)
    except Exception:
        logger.warning("Failed to count live queued receipts for session %s", source_session.id, exc_info=True)
        return None


async def _record_live_input_receipt_for_row(
    *,
    source_session,
    owner_id: int,
    row: SessionInput,
    status_value: str | None = None,
) -> str | None:
    return await record_live_input_receipt_best_effort(
        owner_id=owner_id,
        session_id=source_session.id,
        provider=str(getattr(source_session, "provider", "") or "unknown"),
        device_id=str(getattr(source_session, "device_id", "") or "").strip() or None,
        thread_id=getattr(row, "thread_id", None),
        text=str(getattr(row, "body", "") or ""),
        intent=str(getattr(row, "intent", "") or INPUT_INTENT_AUTO),
        status=str(status_value or getattr(row, "status", "") or "created"),
        client_request_id=getattr(row, "client_request_id", None),
        archive_session_input_id=int(row.id),
        delivery_request_id=getattr(row, "delivery_request_id", None),
    )


async def _record_live_input_receipt_for_body(
    *,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    client_request_id: str,
    intent: InputIntent,
    status_value: str,
    delivery_request_id: str | None = None,
    enqueue_archive_projection: bool = False,
    error: dict[str, object] | None = None,
) -> str | None:
    return await record_live_input_receipt_best_effort(
        owner_id=owner_id,
        session_id=source_session.id,
        provider=str(getattr(source_session, "provider", "") or "unknown"),
        device_id=str(getattr(source_session, "device_id", "") or "").strip() or None,
        thread_id=getattr(source_session, "thread_id", None),
        text=body.text,
        intent=intent,
        status=status_value,
        client_request_id=client_request_id,
        delivery_request_id=delivery_request_id,
        enqueue_archive_projection=enqueue_archive_projection,
        error=error,
    )


def _live_receipt_response(
    *,
    source_session,
    db: Session,
    receipt: LiveInputReceiptSnapshot,
) -> SessionInputResponse:
    recent = _recent_input_summaries(source_session, db)
    return SessionInputResponse(
        outcome="sent" if receipt.status == INPUT_STATUS_DELIVERED else "queued",
        input_id=receipt.archive_session_input_id,
        live_input_id=receipt.id,
        client_request_id=receipt.client_request_id,
        intent=receipt.intent if receipt.intent in (INPUT_INTENT_AUTO, INPUT_INTENT_QUEUE, INPUT_INTENT_STEER) else INPUT_INTENT_AUTO,
        queued=recent,
    )


def _project_live_input_to_archive(
    db: Session,
    *,
    source_session_id,
    owner_id: int,
    text: str,
    intent: InputIntent,
    client_request_id: str,
    delivery_request_id: str,
) -> int:
    return project_session_input_receipt_to_archive(
        db,
        source_session_id=source_session_id,
        owner_id=owner_id,
        text=text,
        intent=intent,
        client_request_id=client_request_id,
        delivery_request_id=delivery_request_id,
    )


def _client_request_id_for_input(body: SessionInputRequest) -> str:
    client_request_id = (body.client_request_id or "").strip()
    if client_request_id:
        return client_request_id
    return uuid.uuid4().hex


def _input_conflict(existing: SessionInput, *, reason: InputConflictReason) -> HTTPException:
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
    recent = _recent_input_summaries(source_session, db)
    outcome = "sent" if existing.status == INPUT_STATUS_DELIVERED else "queued"
    return SessionInputResponse(
        outcome=outcome,
        input_id=int(existing.id),
        client_request_id=existing.client_request_id,
        intent=existing.intent,
        queued=recent,
    )


def _create_session_input_or_existing(
    *,
    db: Session,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    intent: InputIntent,
    status_value: InputStatus,
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

    Do not silently fall back to queue when the user chose intent=steer: the
    intent is corrective, and a silent queue could cause the message to land
    later than desired without the user noticing.
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
    if not _session_has_active_steer_turn(db, source_session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "turn_not_active",
                "message": "This session is not currently in an active turn. Queue this as the next message instead?",
            },
        )

    if retry_row is None and _live_input_store_available():
        live_input_id = await _record_live_input_receipt_for_body(
            source_session=source_session,
            owner_id=owner_id,
            body=body,
            client_request_id=client_request_id,
            intent=INPUT_INTENT_STEER,
            status_value=INPUT_STATUS_DELIVERING,
            delivery_request_id=delivery_request_id,
        )
        if live_input_id is not None:
            result = await steer_text_to_managed_local_session(
                db=db,
                owner_id=owner_id,
                session=source_session,
                text=body.text,
                request_id=delivery_request_id,
            )
            if result.ok:
                live_input_id = await _record_live_input_receipt_for_body(
                    source_session=source_session,
                    owner_id=owner_id,
                    body=body,
                    client_request_id=client_request_id,
                    intent=INPUT_INTENT_STEER,
                    status_value=INPUT_STATUS_DELIVERED,
                    delivery_request_id=delivery_request_id,
                    enqueue_archive_projection=True,
                )
                return SessionInputResponse(
                    outcome="sent",
                    input_id=None,
                    live_input_id=live_input_id,
                    client_request_id=client_request_id,
                    intent=INPUT_INTENT_STEER,
                    queued=_recent_input_summaries(source_session, db),
                )

            error_message = str(result.error or "Managed local steer failed")
            await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_STEER,
                status_value=INPUT_STATUS_FAILED,
                delivery_request_id=delivery_request_id,
                error={"message": error_message[:500]},
            )
            if result.error == MANAGED_LOCAL_STEER_TURN_ENDED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error_code": "turn_ended",
                        "message": "The active turn already ended. Queue this as the next message instead?",
                    },
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error_code": "steer_failed",
                    "message": error_message,
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
        request_id=delivery_request_id,
    )

    if result.ok:
        from zerg.services.session_inputs import mark_delivered as _mark_input_delivered

        _mark_input_delivered(db, int(row.id))
        recent = _recent_input_summaries(source_session, db)
        live_input_id = await _record_live_input_receipt_for_row(
            source_session=source_session,
            owner_id=owner_id,
            row=row,
            status_value=INPUT_STATUS_DELIVERED,
        )
        return SessionInputResponse(
            outcome="sent",
            input_id=int(row.id),
            live_input_id=live_input_id,
            client_request_id=row.client_request_id,
            intent=INPUT_INTENT_STEER,
            queued=recent,
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


def _session_has_active_steer_turn(db: Session, source_session) -> bool:
    session_id = getattr(source_session, "id", None)
    if session_id is None:
        return False
    presence_state = current_presence_state_for_session(db, session_id, session=source_session)
    return str(presence_state or "").strip() in _STEER_ACTIVE_PRESENCE_STATES


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

    if existing_input is None:
        existing_live_receipt = await load_live_input_receipt_by_client_request_best_effort(
            owner_id=owner_id,
            session_id=source_session.id,
            client_request_id=client_request_id,
        )
        if existing_live_receipt is not None:
            if existing_live_receipt.text != body.text:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error_code": "input_conflict",
                        "existing_live_input_id": existing_live_receipt.id,
                        "reason": "different_text",
                    },
                )
            if existing_live_receipt.status in (
                INPUT_STATUS_DELIVERED,
                INPUT_STATUS_QUEUED,
                INPUT_STATUS_DELIVERING,
            ):
                return _live_receipt_response(
                    source_session=source_session,
                    db=db,
                    receipt=existing_live_receipt,
                )

    _assert_no_answerable_pause_request_pending(db=db, source_session=source_session)

    def _cap_check_or_raise() -> None:
        current = _count_active_live_inputs(source_session)
        if current is None:
            current = count_queued(db, source_session.id)
        if current >= MAX_QUEUED_PER_SESSION:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(f"Too many queued inputs for this session ({current}); cancel one before queuing another"),
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
        if existing_input is None and _live_input_store_available():
            live_input_id = await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_QUEUE,
                status_value=INPUT_STATUS_QUEUED,
            )
            if live_input_id is not None:
                return SessionInputResponse(
                    outcome="queued",
                    input_id=None,
                    live_input_id=live_input_id,
                    client_request_id=client_request_id,
                    intent=INPUT_INTENT_QUEUE,
                    queued=_recent_input_summaries(source_session, db),
                )
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
        recent = _recent_input_summaries(source_session, db)
        live_input_id = await _record_live_input_receipt_for_row(
            source_session=source_session,
            owner_id=owner_id,
            row=row,
            status_value=INPUT_STATUS_QUEUED,
        )
        return SessionInputResponse(
            outcome="queued",
            input_id=int(row.id),
            live_input_id=live_input_id,
            client_request_id=row.client_request_id,
            intent=INPUT_INTENT_QUEUE,
            queued=recent,
        )

    # Auto: try to send now; if the session is locked, persist as queued.
    lock_scope_id = session_lock_scope_id(source_session.id)
    lock = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder=delivery_request_id,
        ttl_seconds=300,
    )
    if not lock:
        _cap_check_or_raise()
        if existing_input is None and _live_input_store_available():
            live_input_id = await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_QUEUED,
            )
            if live_input_id is not None:
                return SessionInputResponse(
                    outcome="queued",
                    input_id=None,
                    live_input_id=live_input_id,
                    client_request_id=client_request_id,
                    intent=INPUT_INTENT_AUTO,
                    queued=_recent_input_summaries(source_session, db),
                )
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
        recent = _recent_input_summaries(source_session, db)
        live_input_id = await _record_live_input_receipt_for_row(
            source_session=source_session,
            owner_id=owner_id,
            row=row,
            status_value=INPUT_STATUS_QUEUED,
        )
        return SessionInputResponse(
            outcome="queued",
            input_id=int(row.id),
            live_input_id=live_input_id,
            client_request_id=row.client_request_id,
            intent=INPUT_INTENT_AUTO,
            queued=recent,
        )

    # Lock acquired: prefer a hot-lane receipt for the immediate ACK. If live
    # receipts are unavailable, fall back to the archive SessionInput row path.
    live_input_id = None
    if existing_input is None:
        live_input_id = await _record_live_input_receipt_for_body(
            source_session=source_session,
            owner_id=owner_id,
            body=body,
            client_request_id=client_request_id,
            intent=INPUT_INTENT_AUTO,
            status_value=INPUT_STATUS_DELIVERING,
            delivery_request_id=delivery_request_id,
        )

    try:
        if live_input_id is not None:
            created: SessionInput | SessionInputResponse | None = None
        elif existing_input is not None:
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
            session_input_id=(int(row.id) if row is not None else None),
        )
    except asyncio.CancelledError:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if row is not None:
            _mark_input_failed(db, int(row.id), error="request timed out")
        elif live_input_id is not None:
            await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_FAILED,
                delivery_request_id=delivery_request_id,
            )
        logger.warning(
            "[%s] Session input dispatch cancelled for %s; marked input failed and released lock",
            delivery_request_id,
            source_session.id,
        )
        raise
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if row is not None:
            _mark_input_failed(db, int(row.id), error="dispatch rejected")
        elif live_input_id is not None:
            await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_FAILED,
                delivery_request_id=delivery_request_id,
            )
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, delivery_request_id)
        if row is not None:
            _mark_input_failed(db, int(row.id), error=str(exc)[:200])
        elif live_input_id is not None:
            await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_FAILED,
                delivery_request_id=delivery_request_id,
            )
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
        if row is not None:
            _mark_input_failed(db, int(row.id), error=response_error_message[:200])
        elif live_input_id is not None:
            await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_FAILED,
                delivery_request_id=delivery_request_id,
            )
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

    if row is not None:
        _mark_input_delivered(db, int(row.id))
        recent = _recent_input_summaries(source_session, db)
        live_input_id = await _record_live_input_receipt_for_row(
            source_session=source_session,
            owner_id=owner_id,
            row=row,
            status_value=INPUT_STATUS_DELIVERED,
        )
        input_id = int(row.id)
    else:
        live_input_id = await _record_live_input_receipt_for_body(
            source_session=source_session,
            owner_id=owner_id,
            body=body,
            client_request_id=client_request_id,
            intent=INPUT_INTENT_AUTO,
            status_value=INPUT_STATUS_DELIVERED,
            delivery_request_id=delivery_request_id,
            enqueue_archive_projection=True,
        )
        recent = _recent_input_summaries(source_session, db)
        input_id = None
    return SessionInputResponse(
        outcome="sent",
        input_id=input_id,
        live_input_id=live_input_id,
        client_request_id=(row.client_request_id if row is not None else client_request_id),
        intent=INPUT_INTENT_AUTO,
        queued=recent,
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
    rows = _recent_input_summaries(source_session, db)

    # Cheap stable hash of the state that matters to the client. If none of
    # id/status/updated_at/last_error changed, neither did the chip.
    hasher = blake2b(digest_size=12)
    for r in rows:
        hasher.update(f"{r.id}:{r.live_input_id}:{r.status}:{r.created_at}:{r.last_error or ''}|".encode())
    etag = f'W/"inputs-{hasher.hexdigest()}"'

    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    return rows


@router.delete("/{session_id}/inputs/live/{live_input_id}")
async def cancel_live_session_input_endpoint(
    session_id: str,
    live_input_id: str,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_browser_route_user),
) -> dict:
    source_session = _load_session_for_continuation(db, session_id)
    if not _live_input_store_available():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live input queue is not configured",
        )
    live_ws = get_live_write_serializer()
    if not live_ws.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="live input queue writer is not configured",
        )
    row = await live_ws.execute(
        lambda live_db: cancel_live_queued_receipt(
            live_db,
            session_id=source_session.id,
            receipt_id=live_input_id,
        ),
        auto_commit=False,
        label="live-session-input-cancel",
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="input is no longer queued",
        )
    return {"cancelled": True, "live_input_id": row.id, "input_id": row.archive_session_input_id}


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
