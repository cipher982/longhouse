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
from uuid import UUID

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

import zerg.database as database_module  # noqa: F401  # tests_lite monkeypatches session_chat.database_module
from zerg.auth.caller import Caller
from zerg.auth.caller import caller_principal
from zerg.config import get_settings
from zerg.database import catalog_db_dependency
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.browser_route_auth import get_current_browser_route_caller
from zerg.models.agents import SessionInput
from zerg.models.device_token import DeviceToken
from zerg.routers import agents_sessions as _sessions_router
from zerg.services.console_sessions import create_empty_console_session
from zerg.services.console_turns import ConsoleTurnConflict
from zerg.services.console_turns import ConsoleTurnUnavailable
from zerg.services.console_turns import create_branch_with_first_turn
from zerg.services.console_turns import enqueue_catalog_console_turn
from zerg.services.console_turns import interrupt_console_turn
from zerg.services.live_archive_outbox import project_session_input_receipt_to_archive
from zerg.services.live_session_inputs import LiveInputReceiptSnapshot
from zerg.services.live_session_inputs import cancel_live_queued_receipt_catalog
from zerg.services.live_session_inputs import list_recent_live_input_receipts_catalog
from zerg.services.live_session_inputs import load_live_input_receipt_by_client_request_best_effort
from zerg.services.live_session_inputs import record_live_input_receipt_best_effort
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_local_control import answer_pause_request_on_managed_local_session
from zerg.services.managed_local_launcher import ManagedLocalLaunchError
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan
from zerg.services.managed_local_launcher import managed_local_run_id_for_session
from zerg.services.managed_local_launcher import resolve_managed_local_launch_runner
from zerg.services.session_chat_impl import ManagedLocalSessionLaunchResponse
from zerg.services.session_chat_impl import SessionDraftReplyResponse
from zerg.services.session_chat_impl import SessionLockInfo
from zerg.services.session_chat_impl import _acquire_session_lock_or_raise
from zerg.services.session_chat_impl import _assert_live_session_action_available
from zerg.services.session_chat_impl import _assert_live_session_send_available
from zerg.services.session_chat_impl import _authorize_live_send
from zerg.services.session_chat_impl import _build_managed_local_chat_response
from zerg.services.session_chat_impl import _build_managed_local_draft_reply_response
from zerg.services.session_chat_impl import _load_session_for_continuation
from zerg.services.session_chat_impl import _lock_scope_id_for_session
from zerg.services.session_chat_impl import _managed_local_launch_response_from_plan
from zerg.services.session_chat_impl import _resolve_agents_owner_id
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
from zerg.services.session_inputs import create_session_input
from zerg.services.session_inputs import retry_failed_input
from zerg.services.session_kernel_projection import session_lock_scope_id
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_pause_requests import PENDING_STATUS as PAUSE_PENDING_STATUS
from zerg.services.session_pause_requests import REPLY_TRANSPORT_CLAUDE_PULL
from zerg.services.session_pause_requests import REPLY_TRANSPORT_CURSOR_POLL
from zerg.services.session_views import SessionPauseRequestProjectionResponse
from zerg.session_loop_mode import SessionLoopMode
from zerg.session_loop_mode import coerce_session_loop_mode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])
_catalog_db_dependency = catalog_db_dependency()
_STEER_ACTIVE_PRESENCE_STATES = frozenset({"thinking", "running"})
_MANAGED_LOCAL_HOT_LAUNCH_LEASE_SECS = 300


def _no_catalog_control_db():
    """Catalog-owned control routes receive no request-scoped SQLite session."""

    yield None


# Selected once while routes are registered so FastAPI's dependency override
# machinery has a stable callable to key on.
_catalog_control_db_dependency = _no_catalog_control_db


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SessionMessageRequest(BaseModel):
    """Request to send one message into an explicit session interaction path."""

    message: str = Field(..., min_length=1, max_length=10000, description="User message")


class SessionDraftReplyRequest(BaseModel):
    """Request a suggested next user message without sending it."""

    max_chars: int = Field(1200, ge=100, le=4000, description="Maximum draft length")


class ConsoleSessionCreateRequest(BaseModel):
    device_id: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    cwd: str = Field(..., min_length=1)
    project: str | None = None
    display_name: str | None = None
    launch_surface: str = "web"


class ConsoleSessionCreateResponse(BaseModel):
    session_id: str
    thread_id: str
    created: bool


class SessionBranchCreateRequest(BaseModel):
    message: str = Field(..., min_length=1)
    client_request_id: str = Field(..., min_length=1)
    display_name: str | None = None
    launch_surface: str = "web"


class SessionBranchCreateResponse(BaseModel):
    session_id: str
    thread_id: str
    turn_id: str
    run_id: str | None
    state: str
    created: bool


async def _launch_managed_local_session_serialized(
    db: Session,
    params: ManagedLocalLaunchParams,
    *,
    resume_attempt_id: uuid.UUID | None = None,
    provider_thread_id: str | None = None,
) -> tuple[Any, ManagedLocalSessionLaunchResponse]:
    runner = None if not params.require_runner_ready else resolve_managed_local_launch_runner(db, params)
    plan = build_managed_local_launch_plan(params, runner=runner)
    # Validate the provider-specific response contract before persisting the
    # launch. Catalogd remains the authority for the returned run identity.
    if resume_attempt_id is not None:
        from zerg.services.managed_local_launcher import managed_local_resume_run_id

        planned_run_id = str(managed_local_resume_run_id(plan.session_id, resume_attempt_id))
    else:
        planned_run_id = str(managed_local_run_id_for_session(plan.session_id))
    _managed_local_launch_response_from_plan(plan, run_id=planned_run_id)
    run_id = await _write_hot_managed_local_launch_readiness(
        plan,
        owner_id=params.owner_id,
        git_repo=params.git_repo,
        git_branch=params.git_branch,
        resume_attempt_id=resume_attempt_id,
        provider_thread_id=provider_thread_id,
    )
    launch_response = _managed_local_launch_response_from_plan(plan, run_id=run_id, owner_id=params.owner_id)
    return None, launch_response


async def _write_hot_managed_local_launch_readiness(
    plan,
    *,
    owner_id: int,
    git_repo: str | None,
    git_branch: str | None,
    resume_attempt_id: uuid.UUID | None = None,
    provider_thread_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_MANAGED_LOCAL_HOT_LAUNCH_LEASE_SECS)
    from zerg.catalogd.client import MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise ManagedLocalLaunchError(
            "Managed local launch is blocked because catalogd is unavailable; retry shortly.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    launch = {
        "owner_id": int(owner_id),
        "git_repo": git_repo,
        "git_branch": git_branch,
        "started_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "plan": {
            "session_id": str(plan.session_id),
            "provider": plan.provider,
            "provider_session_id": plan.provider_session_id,
            "source_name": plan.source_name,
            "source_runner_id": plan.source_runner_id,
            "cwd": plan.cwd,
            "project": plan.project,
            "display_name": plan.display_name,
            "managed_session_name": plan.managed_session_name,
            "loop_mode": plan.loop_mode,
            "permission_mode": plan.permission_mode,
            "launch_actor": plan.launch_actor,
            "launch_surface": plan.launch_surface,
            "environment": plan.environment,
            "origin_kind": plan.origin_kind,
            "hidden_from_default_timeline": plan.hidden_from_default_timeline,
            "managed_transport": plan.managed_transport,
            "attach_command": plan.attach_command,
            "provider_config": plan.provider_config,
        },
    }
    try:
        if resume_attempt_id is not None:
            result = await catalogd.call(
                "session.launch.local.resume.v2",
                {
                    "resume": {
                        "owner_id": int(owner_id),
                        "session_id": str(plan.session_id),
                        "provider": plan.provider,
                        "provider_thread_id": provider_thread_id,
                        "device_id": plan.source_name,
                        "cwd": plan.cwd,
                        "launch_actor": plan.launch_actor,
                        "launch_surface": plan.launch_surface,
                        "environment": plan.environment,
                        "origin_kind": plan.origin_kind,
                        "hidden_from_default_timeline": plan.hidden_from_default_timeline,
                        "resume_attempt_id": str(resume_attempt_id),
                        "started_at": now.isoformat(),
                        "expires_at": expires_at.isoformat(),
                    }
                },
                timeout_seconds=MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS,
            )
        else:
            result = await catalogd.call(
                "session.launch.local.create.v2",
                {"launch": launch},
                timeout_seconds=MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS,
            )
        run_id = str(result.get("run_id") or "").strip()
        if not run_id:
            raise RuntimeError("catalogd local launch response is missing run_id")
        catalog_provider_session_id = str(result.get("provider_session_id") or "").strip() or None
        expected_provider_session_id = str(plan.provider_session_id or "").strip() or None
        if catalog_provider_session_id != expected_provider_session_id:
            raise RuntimeError("catalogd local launch response returned a different provider_session_id")
        return run_id
    except CatalogUnavailable as exc:
        raise ManagedLocalLaunchError(
            "Managed local launch is blocked because catalogd is unavailable; retry shortly.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    except CatalogRemoteError as exc:
        if exc.code == "conflict":
            raise ManagedLocalLaunchError(
                str(exc) or "Managed local launch conflicts with an existing launch identity.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if exc.retryable:
            raise ManagedLocalLaunchError(
                str(exc) or "Managed local launch is blocked because catalogd is temporarily unavailable; retry shortly.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        # Server-built launch payloads that fail catalog validation are
        # programmer/contract bugs, not client retries.
        logger.exception("Managed local catalog launch rejected by catalogd")
        raise ManagedLocalLaunchError(
            str(exc) or "Managed local launch is blocked because catalogd rejected launch state.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from exc
    except Exception as exc:
        logger.exception("Managed local catalog launch transaction failed")
        raise ManagedLocalLaunchError(
            "Managed local launch is blocked because catalogd could not persist launch state.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from exc


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
        description=(
            "Managed permission policy: 'bypass', 'provider_local', or 'remote_approve' (answer permission prompts via Longhouse)"
        ),
    )
    provider_config: dict[str, object] | None = Field(
        None,
        description="Provider-specific launch config stored on the thread and spread into Console turn dispatch",
    )
    launch_actor: str | None = Field(None, description="Positive launch actor provenance when known")
    launch_surface: str | None = Field(None, description="Launch surface provenance when known")
    session_id: uuid.UUID | None = Field(
        None,
        description=(
            "Optional client-minted session UUID for Degraded Helm. Retries and later "
            "convergence must reuse this identity instead of minting a replacement."
        ),
    )
    provider_session_id: str | None = Field(
        None,
        min_length=1,
        max_length=512,
        description=("Optional client-minted provider-native identity for degraded Helm convergence"),
    )
    resume_attempt_id: uuid.UUID | None = Field(
        None,
        description="Idempotency identity for an explicit managed-session resume",
    )
    provider_thread_id: str | None = Field(
        None,
        min_length=1,
        max_length=512,
        description="Provider thread identity that the resumed run must retain",
    )


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


class ConsoleTurnReceiptResponse(BaseModel):
    turn_id: str
    run_id: str | None = None
    state: str


class SessionInputResponse(BaseModel):
    """Shape returned from POST /api/sessions/{id}/input."""

    outcome: InputOutcome = Field(..., description="sent | queued")
    input_id: int | None = None
    live_input_id: str | None = None
    client_request_id: str | None = None
    turn: ConsoleTurnReceiptResponse | None = None
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


async def _catalog_interactions(*, session_id: uuid.UUID, status_filter: str | None) -> list[dict[str, Any]]:
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction catalog unavailable")
    try:
        result = await catalogd.call(
            "interaction.list.v2",
            {"session_id": str(session_id), "status": status_filter, "limit": 20},
            timeout_seconds=1.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction catalog unavailable") from exc
    return [item for item in result.get("interactions", []) if isinstance(item, dict)]


def _interaction_projection(interaction: dict[str, Any]) -> dict[str, Any]:
    projection = dict(interaction.get("projection") or {})
    projection["status"] = interaction.get("status")
    projection["can_respond"] = interaction.get("can_respond") is True
    projection["resolved_at"] = interaction.get("resolved_at")
    return projection


async def _resolve_catalog_interaction(
    *,
    session_id: uuid.UUID,
    interaction_id: str,
    status_value: str,
    response_payload: dict[str, Any],
    response_text: str | None,
) -> dict[str, Any]:
    from zerg.catalogd.client import MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction catalog unavailable")
    resolved_at = datetime.now(timezone.utc)
    try:
        result = await catalogd.call(
            "interaction.resolve.v2",
            {
                "session_id": str(session_id),
                "interaction_id": interaction_id,
                "status": status_value,
                "response_payload": response_payload,
                "response_text": response_text,
                "resolved_at": resolved_at.isoformat(),
            },
            # Answering a provider question is a user-initiated mutation, not a
            # hot read. Under the read budget, ordinary write latency reported
            # an answer that had actually been recorded as a failure.
            timeout_seconds=MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS,
        )
    except CatalogUnavailable as exc:
        if exc.outcome_unknown:
            # The answer may already be recorded. Saying it failed sends the
            # user back to retry, and the retry meets the 409 below telling
            # them it has already resolved -- two contradictory messages for a
            # decision that worked.
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    "Your answer may already have been recorded; the catalog did not answer in time. "
                    "Re-read this request before answering again."
                ),
            ) from exc
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction resolution failed") from exc
    except CatalogRemoteError as exc:
        # Preserve what the catalog actually said. A bare `except Exception`
        # collapsed conflict and not_found into a generic 503 and hid genuine
        # programming errors behind an infrastructure-shaped message.
        status_code = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "forbidden": status.HTTP_403_FORBIDDEN,
            "conflict": status.HTTP_409_CONFLICT,
        }.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE)
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    reason = result.get("reason")
    if result.get("found") is not True:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pause request not found for this session")
    if result.get("resolved") is not True:
        code = "pause_request_not_answerable" if reason == "not_answerable" else "pause_request_not_pending"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": code,
                "error_code": code,
                "message": "Answer this request in the terminal."
                if reason == "not_answerable"
                else "This provider question has already resolved.",
                "pause_request_id": interaction_id,
            },
        )
    return result["interaction"]


async def _list_pause_requests_response(
    *,
    source_session,
    status_filter: str | None,
    db: Session,
) -> PauseRequestListResponse:
    interactions = await _catalog_interactions(session_id=source_session.id, status_filter=status_filter)
    requests = [_interaction_projection(item) for item in interactions]
    return PauseRequestListResponse(requests=requests, total=len(requests))


async def _respond_to_pause_request(
    *,
    source_session,
    owner_id: int,
    pause_request_id: str,
    body: PauseRequestResponseRequest,
    db: Session,
) -> PauseRequestResponseResponse:
    return await _respond_to_live_pause_request(
        source_session=source_session,
        owner_id=owner_id,
        pause_request_id=pause_request_id,
        body=body,
        db=db,
    )


async def _respond_to_live_pause_request(
    *,
    source_session,
    owner_id: int,
    pause_request_id: str,
    body: PauseRequestResponseRequest,
    db: Session,
) -> PauseRequestResponseResponse:
    interactions = await _catalog_interactions(session_id=source_session.id, status_filter=None)
    interaction = next((item for item in interactions if str(item.get("id") or "") == pause_request_id), None)
    if interaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pause request not found for this session")
    projection = _interaction_projection(interaction)
    if interaction.get("status") != PAUSE_PENDING_STATUS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "pause_request_not_pending",
                "error_code": "pause_request_not_pending",
                "message": "This provider question has already resolved.",
                "pause_request_id": pause_request_id,
            },
        )
    if not bool(interaction.get("can_respond")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "pause_request_not_answerable",
                "error_code": "pause_request_not_answerable",
                "message": "Answer this request in the terminal.",
                "pause_request_id": pause_request_id,
            },
        )
    decision = str(body.decision or "answer").strip().lower() or "answer"
    if decision not in {"answer", "reject", "cancel"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="decision must be answer, reject, or cancel")
    answers = dict(body.answers or {})
    message = str(body.message or body.content or "").strip() or None
    if message is None and answers:
        labels = {
            str(item.get("id") or ""): str(item.get("question") or item.get("header") or item.get("id") or "")
            for item in projection.get("questions", [])
            if isinstance(item, dict)
        }
        message = (
            "; ".join(
                f"{labels.get(str(key), str(key))}: {', '.join(map(str, value)) if isinstance(value, list) else value}"
                for key, value in answers.items()
            )
            or None
        )
    request_key = str(interaction.get("request_key") or "").strip()
    status_value = "resolved" if decision == "answer" else "rejected"
    if interaction.get("reply_transport") in {REPLY_TRANSPORT_CLAUDE_PULL, REPLY_TRANSPORT_CURSOR_POLL}:
        permission_decision = "allow" if decision == "answer" else "deny"
        resolved = await _resolve_catalog_interaction(
            session_id=source_session.id,
            interaction_id=pause_request_id,
            status_value=status_value,
            response_payload={
                "permissionDecision": permission_decision,
                "permissionDecisionReason": message or f"Longhouse {permission_decision}",
                "decision": decision,
            },
            response_text=message,
        )
        return PauseRequestResponseResponse(status=status_value, pause_request=_interaction_projection(resolved))

    result = await answer_pause_request_on_managed_local_session(
        db=db,
        owner_id=owner_id,
        session=source_session,
        request_key=request_key,
        decision=decision,
        answers=answers,
        content=body.content,
        message=message,
        provider_request_id=(
            str(interaction.get("provider_request_id") or "").strip() if interaction.get("source") == "opencode_bridge" else None
        ),
        request_id=f"pause-{pause_request_id}",
    )
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "pause_response_dispatch_failed",
                "error_code": "pause_response_dispatch_failed",
                "message": str(result.error or "Failed to dispatch pause response command"),
                "pause_request_id": pause_request_id,
                "retryable": True,
                "refetch_required": True,
            },
        )
    bridge_response = dict(result.response_data or {})
    response_payload = bridge_response.get("response_payload")
    if not isinstance(response_payload, dict):
        response_payload = {
            "decision": decision,
            "answers": answers,
            "content": body.content,
            "message": message,
            "dispatch_ok": result.ok,
            "exit_code": result.exit_code,
            "bridge_response": bridge_response or None,
        }
    response_text = str(bridge_response.get("response_text") or message or "").strip() or None
    resolved = await _resolve_catalog_interaction(
        session_id=source_session.id,
        interaction_id=pause_request_id,
        status_value=status_value,
        response_payload=response_payload,
        response_text=response_text,
    )
    return PauseRequestResponseResponse(status=status_value, pause_request=_interaction_projection(resolved))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{session_id}/send-live")
async def send_to_live_session(
    session_id: str,
    body: SessionMessageRequest,
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
):
    """Send text into the live managed-local session and return a fast JSON ack."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
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
    db: Session | None = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
):
    """Generate a suggested next user message for a live managed-local session."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
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
    db: Session = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
):
    """Machine-facing explicit live-send surface for managed-local sessions."""
    settings = get_settings()
    principal = caller_principal(device_token)
    resolved_device_token = principal if isinstance(principal, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    # Authorize and resolve the caller before any session read, so the load can
    # be owner-scoped and an unauthorized caller never learns a session exists.
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id, owner_id=owner_id)
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
    db: Session | None = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
):
    """Machine-facing draft-reply surface for managed-local sessions."""
    settings = get_settings()
    principal = caller_principal(device_token)
    resolved_device_token = principal if isinstance(principal, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id, owner_id=owner_id)
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
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> SessionInterruptResponse:
    """Browser-authenticated explicit interrupt for managed-local sessions."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
    _assert_live_session_action_available(db, source_session, action="interrupt", owner_id=current_user.id)
    return await _interrupt_live_session_response(
        db=db,
        owner_id=current_user.id,
        source_session=source_session,
        request_id=request_id,
    )


@router.post("/{session_id}/turns/current/interrupt", response_model=SessionInterruptResponse)
async def interrupt_current_console_turn(
    session_id: UUID,
    db: Session | None = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> SessionInterruptResponse:
    """Interrupt the current headless Console invocation on its owning machine."""
    try:
        result = await interrupt_console_turn(db, owner_id=current_user.id, session_id=session_id)
    except ConsoleTurnUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc
    if not result.dispatched:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"code": "interrupt_failed", "message": result.error})
    return SessionInterruptResponse(interrupt_dispatched=True, session_id=str(session_id))


@agents_router.post("/{session_id}/interrupt-live", response_model=SessionInterruptResponse)
async def interrupt_live_session_agents(
    session_id: str,
    request: Request,
    db: Session = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> SessionInterruptResponse:
    """Machine-facing explicit interrupt for managed-local sessions.

    A successful response means the interrupt command was dispatched on the
    source runner. It does not confirm that the provider stopped the turn.
    """
    settings = get_settings()
    principal = caller_principal(device_token)
    resolved_device_token = principal if isinstance(principal, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id, owner_id=owner_id)
    _assert_live_session_action_available(db, source_session, action="interrupt", owner_id=owner_id)
    return await _interrupt_live_session_response(
        db=db,
        owner_id=owner_id,
        source_session=source_session,
        request_id=request_id,
    )


@agents_router.post("/{session_id}/turns/current/interrupt", response_model=SessionInterruptResponse)
async def interrupt_current_console_turn_agents(
    session_id: UUID,
    request: Request,
    db: Session | None = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> SessionInterruptResponse:
    settings = get_settings()
    principal = caller_principal(device_token)
    resolved_token = principal if isinstance(principal, DeviceToken) else None
    _authorize_live_send(request=request, device_token=resolved_token, auth_disabled=settings.auth_disabled)
    owner_id = _resolve_agents_owner_id(db, resolved_token)
    try:
        result = await interrupt_console_turn(db, owner_id=owner_id, session_id=session_id)
    except ConsoleTurnUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc
    if not result.dispatched:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"code": "interrupt_failed", "message": result.error})
    return SessionInterruptResponse(interrupt_dispatched=True, session_id=str(session_id))


@router.post("/{session_id}/terminate-live", response_model=SessionTerminateResponse)
async def terminate_live_session(
    session_id: str,
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> SessionTerminateResponse:
    """Browser-authenticated explicit terminate for managed-local sessions."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
    _assert_live_session_action_available(db, source_session, action="terminate", owner_id=current_user.id)
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
    db: Session = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> SessionTerminateResponse:
    """Machine-facing explicit terminate for managed-local sessions.

    A successful response means the terminate command was dispatched on the
    source runner (the engine signalled the provider child). It is not a
    confirmation that the OS has reaped the process, though most managed
    transports kill the child synchronously.
    """
    settings = get_settings()
    principal = caller_principal(device_token)
    resolved_device_token = principal if isinstance(principal, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id, owner_id=owner_id)
    _assert_live_session_action_available(db, source_session, action="terminate", owner_id=owner_id)
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
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> PauseRequestListResponse:
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
    return await _list_pause_requests_response(
        source_session=source_session,
        status_filter=status_filter,
        db=db,
    )


@router.post("/{session_id}/pause-requests/{pause_request_id}/response", response_model=PauseRequestResponseResponse)
async def respond_to_pause_request_endpoint(
    session_id: str,
    pause_request_id: str,
    body: PauseRequestResponseRequest,
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> PauseRequestResponseResponse:
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
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
    db: Session = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> PauseRequestListResponse:
    settings = get_settings()
    principal = caller_principal(device_token)
    resolved_device_token = principal if isinstance(principal, DeviceToken) else None
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id, owner_id=owner_id)
    return await _list_pause_requests_response(
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
    db: Session = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> PauseRequestResponseResponse:
    settings = get_settings()
    principal = caller_principal(device_token)
    resolved_device_token = principal if isinstance(principal, DeviceToken) else None
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    source_session = _load_session_for_continuation(db, session_id, owner_id=owner_id)
    return await _respond_to_pause_request(
        source_session=source_session,
        owner_id=owner_id,
        pause_request_id=pause_request_id,
        body=body,
        db=db,
    )


@router.post("/managed-local/this-device", response_model=ManagedLocalSessionLaunchResponse)
async def launch_managed_local_this_device(
    body: ManagedLocalThisDeviceLaunchRequest,
    db: Session = Depends(_catalog_control_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
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

    if (body.resume_attempt_id is None) != (body.provider_thread_id is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resume_attempt_id and provider_thread_id must be supplied together",
        )
    if body.resume_attempt_id is not None and body.session_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="managed resume requires session_id",
        )

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
            session_id=body.session_id,
            provider_session_id=body.provider_session_id or body.provider_thread_id,
            provider_config=body.provider_config,
        )
        # Managed-local launch is user-facing and hot-path critical. Claim live
        # readiness first; the archive row converges through LiveArchiveOutbox.
        if body.resume_attempt_id is None:
            result, launch_response = await _launch_managed_local_session_serialized(db, params)
        else:
            result, launch_response = await _launch_managed_local_session_serialized(
                db,
                params,
                resume_attempt_id=body.resume_attempt_id,
                provider_thread_id=body.provider_thread_id,
            )
    except ManagedLocalLaunchError as exc:
        if db is not None:
            db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception:
        if db is not None:
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


@router.post("/console", response_model=ConsoleSessionCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_console_session_endpoint(
    body: ConsoleSessionCreateRequest,
    db: Session | None = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> ConsoleSessionCreateResponse:
    """Create an empty Console conversation without starting a provider."""

    provider = body.provider.strip().lower()
    capability = f"{provider}.turn_start"
    registry = get_machine_control_channel_registry()
    if not registry.supports(owner_id=int(current_user.id), device_id=body.device_id, capability=capability):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "adapter_unavailable", "message": f"Machine Agent does not advertise {capability}"},
        )
    try:
        created = await create_empty_console_session(
            db,
            owner_id=int(current_user.id),
            provider=provider,
            device_id=body.device_id,
            cwd=body.cwd,
            project=body.project,
            display_name=body.display_name,
            launch_surface=body.launch_surface,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ConsoleSessionCreateResponse(
        session_id=str(created.session_id),
        thread_id=str(created.thread_id),
        created=created.created,
    )


@router.post(
    "/{session_id}/branches",
    response_model=SessionBranchCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session_branch_endpoint(
    session_id: UUID,
    body: SessionBranchCreateRequest,
    response: Response,
    db: Session | None = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> SessionBranchCreateResponse:
    """Branch an ended Helm session into a new Console session on its machine."""

    owner_id = int(current_user.id)
    parent = await asyncio.to_thread(
        _sessions_router.session_detail_payload,
        session_id=session_id,
        response=response,
        db=db,
        _auth=None,
        owner_id=owner_id,
    )

    # One served fact, computed by the projector, read by both the button and
    # this endpoint. Three gates evaluated here would be a second implementation
    # of "can this be branched", and the visible symptom of drift would be a
    # button offering something the server refuses.
    control = parent.session_state.control
    branch_action = control.actions.branch if control is not None else None
    if branch_action is None or branch_action.state != "available":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": branch_action.reason if branch_action is not None else "control_unknown",
                "message": "This session cannot be branched right now",
            },
        )

    try:
        branch = await create_branch_with_first_turn(
            owner_id=owner_id,
            parent_session_id=session_id,
            message=body.message,
            client_request_id=body.client_request_id,
            display_name=body.display_name,
            launch_surface=body.launch_surface,
        )
    except ConsoleTurnConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ConsoleTurnUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    return SessionBranchCreateResponse(
        session_id=str(branch.session_id),
        thread_id=str(branch.thread_id),
        turn_id=str(branch.turn_id),
        run_id=str(branch.run_id) if branch.run_id else None,
        state=branch.state,
        created=branch.created,
    )


@router.get("/{session_id}/lock")
async def get_session_lock_status(
    session_id: str,
    db: Session = Depends(_catalog_control_db_dependency),
    _current_user=Depends(get_current_browser_route_caller),
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


def _live_queued_summary(receipt: LiveInputReceiptSnapshot) -> QueuedInputSummary:
    last_error = None
    if receipt.error_json:
        try:
            payload = json.loads(receipt.error_json)
            if isinstance(payload, dict):
                code = str(payload.get("code") or "").strip()
                message = str(payload.get("message") or "").strip()
                last_error = f"{code}: {message}" if code and message else (message or code or None)
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


async def _catalog_recent_input_summaries(session_id) -> tuple[list[QueuedInputSummary], int] | None:
    state = await list_recent_live_input_receipts_catalog(session_id=session_id)
    if state is None:
        return None
    receipts, queued_count = state
    return [_live_queued_summary(receipt) for receipt in receipts], queued_count


def _recent_input_summaries(source_session, db: Session) -> list[QueuedInputSummary]:
    return []


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
    recent: list[QueuedInputSummary] | None = None,
) -> SessionInputResponse:
    if recent is None:
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


async def _finish_catalog_input_receipt(
    *,
    receipt_id: str,
    delivery_request_id: str,
    error: str | None = None,
) -> None:
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=503, detail="Live input catalog is unavailable")
    try:
        await catalogd.call(
            "session.input.finish.v2",
            {
                "receipt_id": receipt_id,
                "delivery_request_id": delivery_request_id,
                "status": "delivered" if error is None else "failed",
                "error": str(error)[:500] if error else None,
            },
            timeout_seconds=1.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Live input catalog could not finish the receipt") from exc


async def _create_catalog_session_input_response(
    *,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    db: Session,
) -> SessionInputResponse:
    """Live-receipt authoritative input path used when the cold DB is absent."""

    if getattr(source_session, "command_family", None) == "console_turn":
        client_request_id = _client_request_id_for_input(body)
        try:
            turn = await enqueue_catalog_console_turn(
                owner_id=owner_id,
                session_id=uuid.UUID(str(source_session.id)),
                message=body.text,
                client_request_id=client_request_id,
            )
        except ConsoleTurnUnavailable as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
        except ConsoleTurnConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if turn.error:
            raise HTTPException(
                status_code=502,
                detail={"code": turn.error_code or "provider_launch_failed", "message": turn.error},
            )
        return SessionInputResponse(
            outcome="sent" if turn.state == "active" else "queued",
            input_id=None,
            live_input_id=str(turn.turn_id),
            client_request_id=client_request_id,
            turn=ConsoleTurnReceiptResponse(
                turn_id=str(turn.turn_id),
                run_id=str(turn.run_id) if getattr(turn, "run_id", None) is not None else None,
                state=turn.state,
            ),
            intent=INPUT_INTENT_AUTO,
            queued=[],
        )

    if body.intent not in (INPUT_INTENT_AUTO, INPUT_INTENT_QUEUE, INPUT_INTENT_STEER):
        raise HTTPException(status_code=400, detail=f"unknown intent: {body.intent}")
    _assert_live_session_send_available(db, source_session, owner_id=owner_id)
    client_request_id = _client_request_id_for_input(body)
    existing = await load_live_input_receipt_by_client_request_best_effort(
        owner_id=owner_id,
        session_id=source_session.id,
        client_request_id=client_request_id,
    )
    if existing is not None:
        if existing.text != body.text:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "input_conflict",
                    "existing_live_input_id": existing.id,
                    "reason": "different_text",
                },
            )
        if existing.status in (INPUT_STATUS_DELIVERED, INPUT_STATUS_QUEUED, INPUT_STATUS_DELIVERING):
            state = await _catalog_recent_input_summaries(source_session.id)
            recent = state[0] if state is not None else []
            return _live_receipt_response(source_session=source_session, db=db, receipt=existing, recent=recent)

    state = await _catalog_recent_input_summaries(source_session.id)
    if state is None:
        raise HTTPException(status_code=503, detail="Live input catalog is unavailable")
    current = state[1]
    if current >= MAX_QUEUED_PER_SESSION:
        raise HTTPException(status_code=409, detail=f"Too many queued inputs for this session ({current})")

    delivery_request_id = uuid.uuid4().hex
    if body.intent == INPUT_INTENT_QUEUE:
        receipt_id = await _record_live_input_receipt_for_body(
            source_session=source_session,
            owner_id=owner_id,
            body=body,
            client_request_id=client_request_id,
            intent=INPUT_INTENT_QUEUE,
            status_value=INPUT_STATUS_QUEUED,
        )
        if receipt_id is None:
            raise HTTPException(status_code=503, detail="Live input queue is unavailable")
        return SessionInputResponse(
            outcome="queued",
            input_id=None,
            live_input_id=receipt_id,
            client_request_id=client_request_id,
            intent=INPUT_INTENT_QUEUE,
            queued=(await _catalog_recent_input_summaries(source_session.id) or ([], 0))[0],
        )

    if body.intent == INPUT_INTENT_AUTO:
        lock_scope_id = session_lock_scope_id(source_session.id)
        lock = await session_lock_manager.acquire(
            session_id=lock_scope_id,
            holder=delivery_request_id,
            ttl_seconds=300,
        )
        if not lock:
            receipt_id = await _record_live_input_receipt_for_body(
                source_session=source_session,
                owner_id=owner_id,
                body=body,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_AUTO,
                status_value=INPUT_STATUS_QUEUED,
            )
            if receipt_id is None:
                raise HTTPException(status_code=503, detail="Live input queue is unavailable")
            return SessionInputResponse(
                outcome="queued",
                input_id=None,
                live_input_id=receipt_id,
                client_request_id=client_request_id,
                intent=INPUT_INTENT_AUTO,
                queued=(await _catalog_recent_input_summaries(source_session.id) or ([], 0))[0],
            )
    else:
        lock_scope_id = session_lock_scope_id(source_session.id)

    receipt_id = await _record_live_input_receipt_for_body(
        source_session=source_session,
        owner_id=owner_id,
        body=body,
        client_request_id=client_request_id,
        intent=body.intent,
        status_value=INPUT_STATUS_DELIVERING,
        delivery_request_id=delivery_request_id,
    )
    if receipt_id is None:
        if body.intent == INPUT_INTENT_AUTO:
            await session_lock_manager.release(lock_scope_id, delivery_request_id)
        raise HTTPException(status_code=503, detail="Live input receipt writer is unavailable")

    if body.intent == INPUT_INTENT_STEER:
        from zerg.services.managed_local_control import steer_text_to_managed_local_session

        result = await steer_text_to_managed_local_session(
            db=db,
            owner_id=owner_id,
            session=source_session,
            text=body.text,
            request_id=delivery_request_id,
        )
        if not result.ok:
            await _finish_catalog_input_receipt(
                receipt_id=receipt_id,
                delivery_request_id=delivery_request_id,
                error=str(result.error or "steer failed"),
            )
            raise HTTPException(status_code=409, detail={"error_code": str(result.error or "steer_failed")})
    else:
        dispatch_response = await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=owner_id,
            message=body.text,
            request_id=delivery_request_id,
            lock_scope_id=lock_scope_id,
            db=db,
        )
        if int(dispatch_response.status_code) >= 400:
            try:
                payload = json.loads(dispatch_response.body or b"{}")
            except Exception:
                payload = {}
            error = str(payload.get("error") or "send failed")
            await _finish_catalog_input_receipt(
                receipt_id=receipt_id,
                delivery_request_id=delivery_request_id,
                error=error,
            )
            raise HTTPException(status_code=dispatch_response.status_code, detail=payload)

    await _finish_catalog_input_receipt(
        receipt_id=receipt_id,
        delivery_request_id=delivery_request_id,
    )
    return SessionInputResponse(
        outcome="sent",
        input_id=None,
        live_input_id=receipt_id,
        client_request_id=client_request_id,
        intent=body.intent,
        queued=(await _catalog_recent_input_summaries(source_session.id) or ([], 0))[0],
    )


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


async def _create_session_input_response(
    *,
    source_session,
    owner_id: int,
    body: SessionInputRequest,
    db: Session,
) -> SessionInputResponse:
    return await _create_catalog_session_input_response(
        source_session=source_session,
        owner_id=owner_id,
        body=body,
        db=db,
    )


@router.post("/{session_id}/input", response_model=SessionInputResponse)
async def create_session_input_endpoint(
    session_id: str,
    body: SessionInputRequest,
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> SessionInputResponse:
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
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
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
):
    """List queued + recently-failed inputs for the chip UI.

    The web composer polls this every 2s while any row is queued or
    delivering. Most polls return the same shape, so we emit a weak
    ETag derived from the row state tuple and honor If-None-Match →
    304. A 304 is ~1ms vs ~9ms for the full response, which matters
    at the aggregate QPS of many active session-detail pages.
    """
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
    state = await _catalog_recent_input_summaries(source_session.id)
    if state is None:
        raise HTTPException(status_code=503, detail="Live input catalog is unavailable")
    rows = state[0]

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
    db: Session = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> dict:
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
    row = await cancel_live_queued_receipt_catalog(
        session_id=source_session.id,
        receipt_id=live_input_id,
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
    db: Session | None = Depends(_catalog_control_db_dependency),
    current_user: Caller = Depends(get_current_browser_route_caller),
) -> dict:
    source_session = _load_session_for_continuation(db, session_id, owner_id=current_user.id)
    state = await _catalog_recent_input_summaries(source_session.id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "input_catalog_unavailable",
                "message": "The live input catalog is temporarily unavailable.",
            },
        )
    match = next((item for item in state[0] if item.id == input_id and item.live_input_id), None)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "queued_input_not_found",
                "message": "Queued input was not found for this session.",
            },
        )
    row = await cancel_live_queued_receipt_catalog(
        session_id=source_session.id,
        receipt_id=match.live_input_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "input_no_longer_queued",
                "message": "Input is no longer queued.",
            },
        )
    return {"cancelled": True, "live_input_id": row.id, "input_id": row.archive_session_input_id}


@router.delete("/{session_id}/lock")
async def force_release_lock(
    session_id: str,
    db: Session = Depends(_catalog_control_db_dependency),
    _current_user=Depends(get_current_browser_route_caller),
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
