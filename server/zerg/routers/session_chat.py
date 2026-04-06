"""Session control router for explicit live-send and cloud-branch flows.

Enables interactive chat with synced CLI sessions.
Cloud branching now builds Longhouse-owned branch context from the synced
thread, then starts a fresh Claude CLI turn in the target workspace while
managed-local live send injects into the active local session.

Security:
- Workspace path derived server-side from session metadata (not client)
- Per-session locks prevent concurrent branch/send collisions
- Process cancellation on client disconnect
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session

import zerg.services.live_session_dispatch as live_session_dispatch
from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import AgentEvent
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.request_urls import get_request_public_base_url
from zerg.services.agents_store import AgentsStore
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.managed_local_control import MANAGED_LOCAL_CONTROL_STATUS_COMPLETED
from zerg.services.managed_local_control import MANAGED_LOCAL_CONTROL_STATUS_FAILED
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_COMPLETE
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_FAILED
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_PENDING
from zerg.services.managed_local_control import await_managed_local_turn_terminal
from zerg.services.managed_local_control import get_managed_local_control_status_for_phase
from zerg.services.managed_local_control import get_managed_local_latest_hook_runtime_event_id
from zerg.services.managed_local_control import get_managed_local_presence_updated_at
from zerg.services.managed_local_launcher import ManagedLocalLaunchError
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import launch_managed_local_session
from zerg.services.managed_local_turns import create_managed_local_turn
from zerg.services.managed_local_turns import get_managed_local_turn_snapshot
from zerg.services.managed_local_turns import mark_managed_local_turn_failed
from zerg.services.managed_local_turns import mark_managed_local_turn_send_accepted
from zerg.services.managed_local_turns import mark_managed_local_turn_terminal
from zerg.services.managed_local_turns import maybe_mark_managed_local_turn_durable
from zerg.services.managed_local_turns import run_best_effort_managed_local_turn_write
from zerg.services.session_capabilities import build_session_capabilities
from zerg.services.session_continuity import ShipSessionResult
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_continuity import ship_session_to_zerg
from zerg.services.session_continuity import workspace_resolver
from zerg.services.session_views import ManagedLaunchProfileResponse
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_loop_mode import SessionLoopMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])

SESSION_CHAT_BACKEND_ENV = "SESSION_CHAT_BACKEND"
SESSION_CHAT_MODEL_ENV = "SESSION_CHAT_MODEL"
SESSION_CHAT_ZAI_BASE_URL_ENV = "SESSION_CHAT_ZAI_BASE_URL"
SESSION_CHAT_AWS_PROFILE_ENV = "SESSION_CHAT_AWS_PROFILE"
SESSION_CHAT_AWS_REGION_ENV = "SESSION_CHAT_AWS_REGION"
SESSION_CHAT_BACKEND_AMBIENT = "ambient"
SESSION_CHAT_BACKEND_ZAI = "zai"
SESSION_CHAT_BACKEND_BEDROCK = "bedrock"
SESSION_CHAT_BACKEND_ANTHROPIC = "anthropic"
DEFAULT_SESSION_CHAT_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
# Anthropic-ecosystem model defaults for cloud branching (not in models.json —
# these are Claude Code CLI models, not OpenAI-compatible chat models).
# Override at runtime via SESSION_CHAT_MODEL env var.
DEFAULT_SESSION_CHAT_ZAI_MODEL = "glm-5"
DEFAULT_SESSION_CHAT_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
SUPPORTED_SESSION_CHAT_BACKENDS = {
    SESSION_CHAT_BACKEND_AMBIENT,
    SESSION_CHAT_BACKEND_ZAI,
    SESSION_CHAT_BACKEND_BEDROCK,
    SESSION_CHAT_BACKEND_ANTHROPIC,
}
MANAGED_LOCAL_EVENT_TIMEOUT_SECS = 150.0
MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS = 300.0
MANAGED_LOCAL_POLL_INTERVAL_SECS = 0.1
MANAGED_LOCAL_STABLE_POLLS = 1
MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS = 10.0
MANAGED_LOCAL_POST_DURABLE_TERMINAL_GRACE_SECS = 0.5
_MANAGED_LOCAL_TURN_TIMEOUT_MESSAGE = "".join(
    [
        "Message was sent to the managed local session, but Longhouse ",
        "did not observe a completed turn yet.",
    ]
)
_MANAGED_LOCAL_SYNC_PENDING_NOTE = "".join(
    [
        "Managed-local turn completed, but the transcript is still syncing ",
        "to Longhouse.",
    ]
)
_CLOUD_BRANCH_PERSISTENCE_ERROR = "".join(
    [
        "Response completed, but Longhouse could not save the ",
        "cloud branch transcript to the timeline.",
    ]
)
_CLOUD_BRANCH_EMPTY_EVENTS_ERROR = "".join(
    [
        "Response completed, but Longhouse could not extract any new ",
        "timeline events from the cloud branch transcript.",
    ]
)
_CURRENT_SESSION_HEADER = "X-Longhouse-Session-Id"
_CLOUD_BRANCH_CONTEXT_EVENT_LIMIT = 200
_CLOUD_BRANCH_PROMPT_CHAR_BUDGET = 32_000
_CLOUD_BRANCH_EVENT_CHAR_LIMIT = 2_000


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _get_session_chat_backend() -> str:
    backend = os.getenv(SESSION_CHAT_BACKEND_ENV, SESSION_CHAT_BACKEND_AMBIENT).strip().lower()
    if not backend:
        return SESSION_CHAT_BACKEND_AMBIENT
    if backend not in SUPPORTED_SESSION_CHAT_BACKENDS:
        allowed = sorted(SUPPORTED_SESSION_CHAT_BACKENDS)
        raise RuntimeError(f"{SESSION_CHAT_BACKEND_ENV} must be one of {allowed} (got {backend!r})")
    return backend


@dataclass(frozen=True)
class CloudBranchRuntime:
    backend: str
    cmd: list[str]
    env_updates: dict[str, str]
    env_unset: tuple[str, ...] = ()


def _check_claude_binary() -> bool:
    """Check if the claude CLI binary is available on PATH."""
    import shutil

    return shutil.which("claude") is not None


def _build_claude_branch_runtime(*, prompt: str) -> CloudBranchRuntime:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--print",
    ]
    backend = _get_session_chat_backend()
    if not _check_claude_binary():
        raise RuntimeError(
            "Cloud branching currently requires the 'claude' CLI but it is not installed. "
            "Install @anthropic-ai/claude-code (npm install -g @anthropic-ai/claude-code)."
        )

    if backend == SESSION_CHAT_BACKEND_AMBIENT:
        return CloudBranchRuntime(backend=backend, cmd=cmd, env_updates={})

    model = os.getenv(SESSION_CHAT_MODEL_ENV, "").strip()
    if backend == SESSION_CHAT_BACKEND_ZAI:
        api_key = os.getenv("ZAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(f"{SESSION_CHAT_BACKEND_ENV}=zai requires ZAI_API_KEY")
        env_updates = {
            "ANTHROPIC_BASE_URL": os.getenv(SESSION_CHAT_ZAI_BASE_URL_ENV, DEFAULT_SESSION_CHAT_ZAI_BASE_URL).strip()
            or DEFAULT_SESSION_CHAT_ZAI_BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_MODEL": model or DEFAULT_SESSION_CHAT_ZAI_MODEL,
        }
        return CloudBranchRuntime(
            backend=backend,
            cmd=cmd,
            env_updates=env_updates,
            env_unset=("CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY"),
        )

    if backend == SESSION_CHAT_BACKEND_ANTHROPIC:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(f"{SESSION_CHAT_BACKEND_ENV}=anthropic requires ANTHROPIC_API_KEY")
        env_updates = {"ANTHROPIC_API_KEY": api_key}
        if model:
            env_updates["ANTHROPIC_MODEL"] = model
        else:
            env_updates["ANTHROPIC_MODEL"] = DEFAULT_SESSION_CHAT_ANTHROPIC_MODEL
        return CloudBranchRuntime(
            backend=backend,
            cmd=cmd,
            env_updates=env_updates,
            env_unset=("CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"),
        )

    env_updates = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    aws_profile = os.getenv(SESSION_CHAT_AWS_PROFILE_ENV, "").strip()
    aws_region = os.getenv(SESSION_CHAT_AWS_REGION_ENV, "").strip()
    if aws_profile:
        env_updates["AWS_PROFILE"] = aws_profile
    if aws_region:
        env_updates["AWS_REGION"] = aws_region
    if model:
        env_updates["ANTHROPIC_MODEL"] = model
    return CloudBranchRuntime(
        backend=backend,
        cmd=cmd,
        env_updates=env_updates,
        env_unset=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class SessionMessageRequest(BaseModel):
    """Request to send one message into an explicit session interaction path."""

    message: str = Field(..., min_length=1, max_length=10000, description="User message")


class ManagedLocalSessionLaunchRequest(BaseModel):
    """Request to start a managed local AI agent session on a runner."""

    runner_target: str = Field(..., description="Runner name or runner:<id>")
    cwd: str = Field(..., min_length=1, description="Working directory on the source runner")
    provider: str = Field("claude", description="AI provider CLI to launch (claude or codex)")
    project: str | None = Field(None, description="Optional project label")
    git_repo: str | None = Field(None, description="Optional git repository path")
    git_branch: str | None = Field(None, description="Optional git branch name")
    display_name: str | None = Field(None, description="Optional display name for the session")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.MANUAL, description="manual | assist | autopilot")
    claude_launch_env: dict[str, str] | None = Field(
        None,
        description="Optional allowlisted Claude launch env overrides to apply on the runner",
    )


class ManagedLocalThisDeviceLaunchRequest(BaseModel):
    """Request to start a managed local AI agent session on the calling device."""

    cwd: str = Field(..., min_length=1, description="Working directory on this device")
    provider: str = Field("claude", description="AI provider CLI to launch (claude or codex)")
    project: str | None = Field(None, description="Optional project label")
    git_repo: str | None = Field(None, description="Optional git repository path")
    git_branch: str | None = Field(None, description="Optional git branch name")
    display_name: str | None = Field(None, description="Optional display name for the session")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.MANUAL, description="manual | assist | autopilot")
    machine_name: str | None = Field(
        None,
        description="Optional local Longhouse machine label override used to resolve this device's runner",
    )
    native_claude_channels_available: bool | None = Field(
        None,
        description="Optional CLI capability hint for whether native Claude channels are available on this device",
    )
    claude_launch_env: dict[str, str] | None = Field(
        None,
        description="Optional allowlisted Claude launch env overrides to apply on the local runner",
    )


class ManagedLocalSessionLaunchResponse(BaseModel):
    """Response after successfully starting a managed local session."""

    session_id: str
    provider: str
    provider_session_id: str
    execution_home: SessionExecutionHome
    managed_transport: ManagedSessionTransport
    loop_mode: SessionLoopMode
    source_runner_id: int | None = None
    source_runner_name: str
    managed_session_name: str
    attach_command: str
    managed_launch_profile: ManagedLaunchProfileResponse | None = None


class SessionLockInfo(BaseModel):
    """Information about a session lock."""

    locked: bool
    holder: str | None = None
    time_remaining_seconds: float | None = None
    fork_available: bool = False


class SessionChatError(BaseModel):
    """Error response for session chat."""

    error: str
    code: str
    lock_info: SessionLockInfo | None = None


def _resolve_agents_owner_id(db: Session, device_token: DeviceToken | None) -> int:
    owner_id = getattr(device_token, "owner_id", None)
    if owner_id is not None:
        owner = db.query(User.id).filter(User.id == int(owner_id)).first()
        if owner is not None:
            return int(owner[0])
        logger.warning("Device token owner_id=%s is stale; falling back to single-tenant owner", owner_id)

    owner = db.query(User.id).order_by(User.id.asc()).first()
    if owner is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No Longhouse user is configured")
    return int(owner[0])


def _managed_local_launch_response(result) -> ManagedLocalSessionLaunchResponse:
    session = result.session
    managed_launch_profile = getattr(session, "managed_launch_profile", None)
    capabilities = build_session_capabilities(session)
    if capabilities.execution_home != SessionExecutionHome.MANAGED_LOCAL:
        raise RuntimeError("Managed local launch response requires a managed_local session")
    if capabilities.managed_transport is None:
        raise RuntimeError("Managed local launch response is missing managed transport metadata")
    return ManagedLocalSessionLaunchResponse(
        session_id=str(session.id),
        provider=session.provider or "claude",
        provider_session_id=session.provider_session_id or str(session.id),
        execution_home=capabilities.execution_home,
        managed_transport=capabilities.managed_transport,
        loop_mode=SessionLoopMode(session.loop_mode or SessionLoopMode.MANUAL.value),
        source_runner_id=getattr(session, "source_runner_id", None),
        source_runner_name=session.source_runner_name or "",
        managed_session_name=session.managed_session_name or "",
        attach_command=result.attach_command,
        managed_launch_profile=(
            ManagedLaunchProfileResponse.model_validate(managed_launch_profile) if isinstance(managed_launch_profile, dict) else None
        ),
    )


def _session_chat_streaming_response(stream: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _load_session_for_continuation(db: Session, session_id: str):
    try:
        source_session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid session id: {session_id}",
        ) from exc

    source_session = AgentsStore(db).get_session(source_session_uuid)
    if not source_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )
    return source_session


def _trim_cloud_branch_prompt_text(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _format_event_for_cloud_branch_prompt(event: AgentEvent) -> str | None:
    role = str(event.role or "").strip().lower()
    if role == "user":
        text = _trim_cloud_branch_prompt_text(event.content_text, limit=_CLOUD_BRANCH_EVENT_CHAR_LIMIT)
        return f"User: {text}" if text else None
    if role == "assistant":
        text = _trim_cloud_branch_prompt_text(event.content_text, limit=_CLOUD_BRANCH_EVENT_CHAR_LIMIT)
        return f"Assistant: {text}" if text else None

    lines: list[str] = []
    tool_label = str(event.tool_name or "").strip() or "tool"
    if event.tool_input_json is not None:
        lines.append(
            f"{tool_label} input: "
            f"{_trim_cloud_branch_prompt_text(json.dumps(event.tool_input_json, ensure_ascii=True, sort_keys=True), limit=1000)}"
        )
    if event.tool_output_text:
        lines.append(f"{tool_label} output: {_trim_cloud_branch_prompt_text(event.tool_output_text, limit=1500)}")
    if event.content_text:
        label = role.title() or "Event"
        lines.append(f"{label}: {_trim_cloud_branch_prompt_text(event.content_text, limit=1000)}")
    if not lines:
        return None
    return "\n".join(lines)


def build_cloud_branch_prompt(*, db: Session, source_session, message: str) -> str:
    store = AgentsStore(db)
    events = store.get_session_events(
        source_session.id,
        branch_mode="head",
        limit=_CLOUD_BRANCH_CONTEXT_EVENT_LIMIT,
    )
    rendered_events = [line for event in events if (line := _format_event_for_cloud_branch_prompt(event))]
    selected_events: list[str] = []
    remaining_chars = _CLOUD_BRANCH_PROMPT_CHAR_BUDGET
    for line in reversed(rendered_events):
        line_cost = len(line) + 1
        if selected_events and line_cost > remaining_chars:
            break
        if not selected_events and line_cost > remaining_chars:
            selected_events.append(_trim_cloud_branch_prompt_text(line, limit=remaining_chars))
            remaining_chars = 0
            break
        selected_events.append(line)
        remaining_chars -= line_cost
    selected_events.reverse()
    transcript = "\n".join(selected_events) if selected_events else "(No prior transcript events were available.)"
    transcript_note = (
        "Transcript excerpt below includes the latest synced events that fit Longhouse's branch prompt budget.\n"
        if len(selected_events) < len(rendered_events)
        else ""
    )
    project_label = source_session.project or "unknown"
    provider_label = source_session.provider or "unknown"
    workspace_label = source_session.cwd or "unknown"
    branch_label = source_session.git_branch or "unknown"
    return (
        "You are starting a new Longhouse cloud branch from a synced CLI session.\n"
        "Treat the transcript below as the durable prior context.\n"
        "Do not assume hidden provider-local memory beyond what is written here.\n"
        "Continue the work from this context; if something important is missing, say so briefly and proceed from the saved thread.\n\n"
        f"Project: {project_label}\n"
        f"Provider: {provider_label}\n"
        f"Workspace: {workspace_label}\n"
        f"Git branch: {branch_label}\n\n"
        f"{transcript_note}"
        "Transcript:\n"
        f"{transcript}\n\n"
        "New user message:\n"
        f"{message}"
    )


def _assert_cloud_branch_available(source_session) -> None:
    capabilities = build_session_capabilities(source_session)
    provider = str(source_session.provider or "").strip().lower()
    provider_label = "Codex" if provider == "codex" else "Claude" if provider == "claude" else provider or "This"

    if capabilities.cloud_branch_available:
        return
    if capabilities.live_control_available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This session currently has live Longhouse control. Use live send instead of starting a cloud branch.",
        )
    if capabilities.host_reattach_available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This live session needs host attach before Longhouse can start a cloud branch from it.",
        )
    if provider == "codex":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Codex sessions are not yet available for cloud branching from Longhouse.",
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"{provider_label} sessions are not available for cloud branching from Longhouse.",
    )


def _assert_live_session_send_available(source_session) -> None:
    capabilities = build_session_capabilities(source_session)
    if capabilities.live_control_available:
        return
    if capabilities.host_reattach_available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This live session needs host attach before Longhouse can continue it.",
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="This session does not have a live Longhouse control channel.",
    )


def _parse_current_session_header(request: Request) -> UUID | None:
    raw = str(request.headers.get(_CURRENT_SESSION_HEADER, "") or "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{_CURRENT_SESSION_HEADER} must be a valid UUID",
        ) from exc


def _authorize_live_send(
    *,
    request: Request,
    device_token: DeviceToken | None,
    auth_disabled: bool,
) -> None:
    # Accept an optional current-session hint for consistency with other machine
    # surfaces, but live send authorization itself only needs a valid device token.
    _parse_current_session_header(request)

    if device_token is None and not auth_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Machine live send requires a device token",
        )


def _authorize_cloud_branch(
    *,
    request: Request,
    device_token: DeviceToken | None,
    source_session,
    auth_disabled: bool,
) -> None:
    header_session_id = _parse_current_session_header(request)
    if device_token is None:
        if auth_disabled:
            if header_session_id is not None and header_session_id != source_session.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Current session header does not match the target session",
                )
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Machine cloud branching requires a device token",
        )

    if header_session_id is not None and header_session_id != source_session.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current session header does not match the target session",
        )

    token_device_id = str(getattr(device_token, "device_id", "") or "").strip()
    session_device_id = str(getattr(source_session, "device_id", "") or "").strip()
    if token_device_id and session_device_id and token_device_id != session_device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated device cannot start a cloud branch from a session on another device",
        )

    if header_session_id is None and (not token_device_id or token_device_id != session_device_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Machine cloud branching requires current session context or a matching device token",
        )


async def _build_managed_local_chat_response(
    *,
    source_session,
    owner_id: int,
    message: str,
    request_id: str,
    lock_scope_id: str,
    db: Session,
) -> JSONResponse:
    """Dispatch text to a managed-local session and return a fast ack.

    The response appears in the timeline via the normal engine shipping path +
    the session workspace SSE stream (Step 1).  No inline streaming, no
    polling, no force-ship.
    """
    return await _dispatch_managed_local_text(
        source_session=source_session,
        owner_id=owner_id,
        message=message,
        request_id=request_id,
        lock_scope_id=lock_scope_id,
        db=db,
    )


async def _release_managed_local_lock_after_terminal(
    *,
    lock_scope_id: str,
    request_id: str,
    session_id: UUID,
    db_bind,
    after_runtime_event_id: int,
    after_presence_updated_at: datetime | None,
) -> None:
    try:
        terminal_result = await await_managed_local_turn_terminal(
            db_bind=db_bind,
            session_id=session_id,
            after_runtime_event_id=after_runtime_event_id,
            after_presence_updated_at=after_presence_updated_at,
            timeout_secs=MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS,
        )
    except Exception:
        logger.warning(
            "[%s] Managed-local lock watcher crashed for %s",
            request_id,
            session_id,
            exc_info=True,
        )
        return

    if terminal_result is None:
        logger.warning(
            "[%s] Managed-local lock watcher timed out for %s; leaving TTL lock in place",
            request_id,
            session_id,
        )
        return

    released = await session_lock_manager.release(lock_scope_id, request_id)
    logger.info(
        "[%s] Managed-local session reached terminal phase %s; lock release=%s",
        request_id,
        terminal_result.phase,
        released,
    )


def _schedule_managed_local_lock_release(
    *,
    lock_scope_id: str,
    request_id: str,
    session_id: UUID,
    db_bind,
    after_runtime_event_id: int,
    after_presence_updated_at: datetime | None,
) -> None:
    task = asyncio.create_task(
        _release_managed_local_lock_after_terminal(
            lock_scope_id=lock_scope_id,
            request_id=request_id,
            session_id=session_id,
            db_bind=db_bind,
            after_runtime_event_id=after_runtime_event_id,
            after_presence_updated_at=after_presence_updated_at,
        )
    )

    def _log_task_failure(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            logger.debug("[%s] Managed-local lock watcher cancelled for %s", request_id, session_id)
        except Exception:
            logger.exception("[%s] Managed-local lock watcher failed for %s", request_id, session_id)

    task.add_done_callback(_log_task_failure)


async def _dispatch_managed_local_text(
    *,
    source_session,
    owner_id: int,
    message: str,
    request_id: str,
    lock_scope_id: str,
    db: Session,
) -> JSONResponse:
    """Send text to a managed-local session and return acceptance status."""
    t0 = time.monotonic()
    if not build_session_capabilities(source_session).live_control_available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Managed local session is missing live runner metadata",
        )

    baseline_event_id = int(AgentsStore(db).get_latest_event_id(source_session.id) or 0)
    baseline_hook_runtime_event_id = get_managed_local_latest_hook_runtime_event_id(
        db=db,
        session_id=source_session.id,
    )
    baseline_presence_updated_at = get_managed_local_presence_updated_at(session_id=source_session.id)
    t_baseline = time.monotonic()
    run_best_effort_managed_local_turn_write(
        db_bind=db.get_bind(),
        label="create",
        fn=lambda turn_db: create_managed_local_turn(
            turn_db,
            session_id=source_session.id,
            request_id=request_id,
            baseline_event_id=baseline_event_id,
            baseline_runtime_event_id=baseline_hook_runtime_event_id,
            expected_user_text=message,
        ),
    )
    t_turn_created = time.monotonic()
    send_result = await live_session_dispatch.send_text_to_live_session(
        db=db,
        owner_id=owner_id,
        session=source_session,
        text=message,
        commis_id=request_id,
        timeout_secs=15,
        verify_turn_started=True,
        verification_timeout_secs=15.0,
    )
    t_sent = time.monotonic()

    if not send_result.ok or not bool(getattr(send_result, "verified_turn_started", False)):
        run_best_effort_managed_local_turn_write(
            db_bind=db.get_bind(),
            label="send_failed",
            fn=lambda turn_db: mark_managed_local_turn_failed(
                turn_db,
                session_id=source_session.id,
                request_id=request_id,
                error_code="send_failed",
            ),
        )
        error_message = str(send_result.error or "Managed local session did not acknowledge the prompt after send")
        await session_lock_manager.release(lock_scope_id, request_id)
        logger.info(f"[{request_id}] Managed local chat dispatch failed, lock released")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "accepted": False,
                "error": error_message,
                "session_id": str(source_session.id),
                "request_id": request_id,
            },
        )

    run_best_effort_managed_local_turn_write(
        db_bind=db.get_bind(),
        label="send_accepted",
        fn=lambda turn_db: mark_managed_local_turn_send_accepted(
            turn_db,
            session_id=source_session.id,
            request_id=request_id,
        ),
    )

    _schedule_managed_local_lock_release(
        lock_scope_id=lock_scope_id,
        request_id=request_id,
        session_id=source_session.id,
        db_bind=db.get_bind(),
        after_runtime_event_id=baseline_hook_runtime_event_id,
        after_presence_updated_at=baseline_presence_updated_at,
    )

    dispatch_ms = round((t_sent - t0) * 1000, 1)
    logger.info(
        "[%s] managed-local dispatch: baseline=%.0fms turn_create=%.0fms send=%.0fms total=%.0fms",
        request_id,
        (t_baseline - t0) * 1000,
        (t_turn_created - t_baseline) * 1000,
        (t_sent - t_turn_created) * 1000,
        dispatch_ms,
    )

    return JSONResponse(
        content={
            "accepted": True,
            "session_id": str(source_session.id),
            "request_id": request_id,
            "dispatch_ms": dispatch_ms,
        },
    )


async def _build_cloud_branch_response(
    *,
    source_session,
    message: str,
    request_id: str,
    lock_scope_id: str,
    db: Session,
) -> StreamingResponse:
    store = AgentsStore(db)
    target_session, created_branch = store.ensure_cloud_continuation_target(source_session.id)
    inherited_provider_session_id = str(target_session.provider_session_id or "").strip()
    source_provider_session_id = str(source_session.provider_session_id or "").strip()
    if inherited_provider_session_id and inherited_provider_session_id == source_provider_session_id:
        target_session.provider_session_id = None
    branch_prompt = build_cloud_branch_prompt(db=db, source_session=source_session, message=message)
    db.commit()

    resolved_workspace = await workspace_resolver.resolve(
        original_cwd=source_session.cwd,
        git_repo=source_session.git_repo,
        git_branch=source_session.git_branch,
        session_id=str(target_session.id),
    )

    if resolved_workspace.error:
        if resolved_workspace.is_temp:
            resolved_workspace.cleanup()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resolve workspace: {resolved_workspace.error}",
        )

    logger.info(
        f"[{request_id}] Prepared cloud branch context from source session {source_session.id} "
        f"target={target_session.id} workspace={resolved_workspace.path} is_temp={resolved_workspace.is_temp} "
        f"provider=claude prompt_chars={len(branch_prompt)}"
    )

    async def generate():
        try:
            continued_from_session_id = str(target_session.continued_from_session_id) if target_session.continued_from_session_id else None
            async for event in stream_session_cloud_branch_output(
                source_session_id=str(source_session.id),
                target_session_id=str(target_session.id),
                thread_root_session_id=str(target_session.thread_root_session_id or target_session.id),
                continued_from_session_id=continued_from_session_id,
                created_branch=created_branch,
                branched_from_event_id=target_session.branched_from_event_id,
                workspace_path=resolved_workspace.path,
                message=message,
                prompt=branch_prompt,
                request_id=request_id,
                db=db,
            ):
                yield event
        finally:
            await session_lock_manager.release(lock_scope_id, request_id)
            if resolved_workspace.is_temp:
                resolved_workspace.cleanup()
            logger.info(f"[{request_id}] Session chat complete, lock released")

    return _session_chat_streaming_response(generate())


def _lock_scope_id_for_session(db: Session, session_id: str) -> str:
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return session_id
    session = AgentsStore(db).get_session(session_uuid)
    if session is None:
        return session_id
    return str(session.thread_root_session_id or session.id)


async def _acquire_session_lock_or_raise(*, source_session, request_id: str) -> str:
    lock_scope_id = str(source_session.thread_root_session_id or source_session.id)
    lock = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder=request_id,
        ttl_seconds=300,
    )

    if lock:
        return lock_scope_id

    existing_lock = await session_lock_manager.get_lock_info(lock_scope_id)
    lock_info = SessionLockInfo(
        locked=True,
        holder=existing_lock.holder if existing_lock else None,
        time_remaining_seconds=existing_lock.time_remaining if existing_lock else None,
        fork_available=True,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "Session is currently in use",
            "code": "SESSION_LOCKED",
            "lock_info": lock_info.model_dump(),
        },
    )


def _fetch_managed_local_events_since(*, db_bind, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id > after_event_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


def _fetch_managed_local_events_between_ids(
    *,
    db_bind,
    session_id: UUID,
    start_event_id: int,
    end_event_id: int,
) -> list[AgentEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id >= int(start_event_id))
            .filter(AgentEvent.id <= int(end_event_id))
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


def _get_managed_local_turn_snapshot_best_effort(
    *,
    db_bind,
    session_id: UUID,
    request_id: str,
):
    try:
        return get_managed_local_turn_snapshot(
            db_bind=db_bind,
            session_id=session_id,
            request_id=request_id,
        )
    except SQLAlchemyTimeoutError:
        logger.warning(
            "Managed-local turn ledger snapshot timed out for %s; falling back to direct evidence",
            session_id,
        )
    except Exception:
        logger.warning(
            "Managed-local turn ledger snapshot read failed for %s; falling back to direct evidence",
            session_id,
            exc_info=True,
        )
    return None


def _hydrate_managed_local_turn_events_from_ledger(
    *,
    db_bind,
    session_id: UUID,
    request_id: str,
    expected_user_message: str,
) -> tuple[object | None, list[AgentEvent]]:
    snapshot = _get_managed_local_turn_snapshot_best_effort(
        db_bind=db_bind,
        session_id=session_id,
        request_id=request_id,
    )
    if (
        snapshot is None
        or snapshot.durable_at is None
        or snapshot.durable_user_event_id is None
        or snapshot.durable_assistant_event_id is None
    ):
        return snapshot, []

    try:
        events = _fetch_managed_local_events_between_ids(
            db_bind=db_bind,
            session_id=session_id,
            start_event_id=int(snapshot.durable_user_event_id),
            end_event_id=int(snapshot.durable_assistant_event_id),
        )
    except SQLAlchemyTimeoutError:
        logger.warning(
            "Managed-local turn ledger event hydration timed out for %s; falling back to direct evidence",
            session_id,
        )
        return snapshot, []
    except Exception:
        logger.warning(
            "Managed-local turn ledger event hydration failed for %s; falling back to direct evidence",
            session_id,
            exc_info=True,
        )
        return snapshot, []
    if expected_user_message and not _managed_local_events_include_expected_turn(
        events=events,
        expected_user_message=expected_user_message,
    ):
        return snapshot, []
    return snapshot, events


def _get_managed_local_latest_event_id(*, db_bind, session_id: UUID) -> int:
    with Session(bind=db_bind) as poll_db:
        latest = AgentsStore(poll_db).get_latest_event_id(session_id)
        return int(latest or 0)


def _managed_local_events_include_expected_turn(*, events: list[AgentEvent], expected_user_message: str) -> bool:
    saw_expected_user_prompt = False

    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        content_text = str(getattr(event, "content_text", "") or "")
        tool_name = str(getattr(event, "tool_name", "") or "").strip()
        if role == "user" and strip_claude_channel_wrapper(content_text) == expected_user_message:
            saw_expected_user_prompt = True
            continue
        if not saw_expected_user_prompt:
            continue
        if tool_name:
            return True
        if role == "assistant" and content_text.strip():
            return True

    return False


async def _await_managed_local_turn_events(
    *,
    db_bind,
    session_id: UUID,
    after_event_id: int,
    expected_user_message: str | None = None,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> list[AgentEvent]:
    deadline = time.monotonic() + timeout_secs
    latest_seen = after_event_id
    stable_polls = 0
    saw_pool_timeout = False

    while time.monotonic() < deadline:
        try:
            latest_event_id = _get_managed_local_latest_event_id(db_bind=db_bind, session_id=session_id)
            if latest_event_id > after_event_id:
                if latest_event_id == latest_seen:
                    stable_polls += 1
                else:
                    latest_seen = latest_event_id
                    stable_polls = 0

                if stable_polls >= MANAGED_LOCAL_STABLE_POLLS:
                    events = _fetch_managed_local_events_since(
                        db_bind=db_bind,
                        session_id=session_id,
                        after_event_id=after_event_id,
                    )
                    if expected_user_message and not _managed_local_events_include_expected_turn(
                        events=events,
                        expected_user_message=expected_user_message,
                    ):
                        await asyncio.sleep(poll_interval_secs)
                        continue
                    return events
        except SQLAlchemyTimeoutError:
            if not saw_pool_timeout:
                logger.warning(
                    "Managed-local event poll for %s timed out waiting for a DB connection; retrying",
                    session_id,
                )
                saw_pool_timeout = True

        await asyncio.sleep(poll_interval_secs)

    return []


async def _await_managed_local_events_task(
    events_task: asyncio.Task[list[AgentEvent]],
    *,
    timeout_secs: float,
) -> list[AgentEvent]:
    if events_task.done():
        return events_task.result() or []
    try:
        return await asyncio.wait_for(asyncio.shield(events_task), timeout=timeout_secs)
    except asyncio.TimeoutError:
        return []


async def _await_managed_local_terminal_task(
    terminal_task: asyncio.Task,
    *,
    timeout_secs: float,
):
    if terminal_task.done():
        try:
            return terminal_task.result()
        except asyncio.CancelledError:
            return None
        except Exception:
            logger.warning("Managed-local terminal waiter failed after durable events", exc_info=True)
            return None
    try:
        return await asyncio.wait_for(asyncio.shield(terminal_task), timeout=timeout_secs)
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        return None
    except Exception:
        logger.warning("Managed-local terminal waiter failed after durable events", exc_info=True)
        return None


async def _stream_managed_local_output(
    *,
    source_session,
    owner_id: int,
    message: str,
    request_id: str,
    db: Session | None = None,
) -> AsyncIterator[str]:
    if db is None:
        raise RuntimeError("Managed local chat requires a database session")
    capabilities = build_session_capabilities(source_session)
    if not capabilities.live_control_available:
        raise RuntimeError("Managed local session is missing live runner metadata")

    yield SSEEvent(
        event="system",
        data=json.dumps(
            {
                "type": "session_started",
                "session_id": str(source_session.id),
                "source_session_id": str(source_session.id),
                "thread_root_session_id": str(source_session.thread_root_session_id or source_session.id),
                "continued_from_session_id": (
                    str(source_session.continued_from_session_id) if source_session.continued_from_session_id else None
                ),
                "created_branch": False,
                "provider_session_id": source_session.provider_session_id,
                "execution_home": capabilities.execution_home.value,
                "origin_label": source_session.origin_label,
                "runner_name": source_session.source_runner_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ),
    ).encode()

    baseline_event_id = int(AgentsStore(db).get_latest_event_id(source_session.id) or 0)
    baseline_hook_runtime_event_id = get_managed_local_latest_hook_runtime_event_id(
        db=db,
        session_id=source_session.id,
    )
    baseline_presence_updated_at = get_managed_local_presence_updated_at(session_id=source_session.id)
    run_best_effort_managed_local_turn_write(
        db_bind=db.get_bind(),
        label="create",
        fn=lambda turn_db: create_managed_local_turn(
            turn_db,
            session_id=source_session.id,
            request_id=request_id,
            baseline_event_id=baseline_event_id,
            baseline_runtime_event_id=baseline_hook_runtime_event_id,
            expected_user_text=message,
        ),
    )
    send_result = await live_session_dispatch.send_text_to_live_session(
        db=db,
        owner_id=owner_id,
        session=source_session,
        text=message,
        commis_id=request_id,
        timeout_secs=15,
    )

    if not send_result.ok:
        run_best_effort_managed_local_turn_write(
            db_bind=db.get_bind(),
            label="send_failed",
            fn=lambda turn_db: mark_managed_local_turn_failed(
                turn_db,
                session_id=source_session.id,
                request_id=request_id,
                error_code="send_failed",
            ),
        )
        error_message = str(send_result.error or "Failed to send text to managed local session")
        yield SSEEvent(
            event="error",
            data=json.dumps({"error": error_message}),
        ).encode()
        yield SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": str(source_session.id),
                    "source_session_id": str(source_session.id),
                    "shipped_session_id": None,
                    "created_branch": False,
                    "control_status": MANAGED_LOCAL_CONTROL_STATUS_FAILED,
                    "sync_status": MANAGED_LOCAL_SYNC_STATUS_FAILED,
                    "exit_code": 1,
                    "total_text_length": 0,
                    "persisted_events": 0,
                    "persistence_error": error_message,
                    "sync_note": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()
        return
    run_best_effort_managed_local_turn_write(
        db_bind=db.get_bind(),
        label="send_accepted",
        fn=lambda turn_db: mark_managed_local_turn_send_accepted(
            turn_db,
            session_id=source_session.id,
            request_id=request_id,
        ),
    )

    terminal_task = asyncio.create_task(
        await_managed_local_turn_terminal(
            db_bind=db.get_bind(),
            session_id=source_session.id,
            after_runtime_event_id=baseline_hook_runtime_event_id,
            after_presence_updated_at=baseline_presence_updated_at,
            timeout_secs=MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
            poll_interval_secs=MANAGED_LOCAL_POLL_INTERVAL_SECS,
        )
    )
    events_task = asyncio.create_task(
        _await_managed_local_turn_events(
            db_bind=db.get_bind(),
            session_id=source_session.id,
            after_event_id=baseline_event_id,
            expected_user_message=message,
        )
    )

    terminal_result = None
    new_events: list[AgentEvent] = []

    try:
        # Wait for terminal phase or events — daemon handles shipping for all providers.
        done, _pending = await asyncio.wait(
            {terminal_task, events_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if events_task in done:
            new_events = events_task.result() or []
        if terminal_task in done:
            terminal_result = terminal_task.result()

        if new_events and terminal_result is None:
            terminal_result = await _await_managed_local_terminal_task(
                terminal_task,
                timeout_secs=MANAGED_LOCAL_POST_DURABLE_TERMINAL_GRACE_SECS,
            )

        if not new_events:
            if terminal_task in done:
                terminal_result = terminal_task.result()
            elif not terminal_task.done():
                terminal_result = await terminal_task

        if not new_events and terminal_result is not None:
            new_events = await _await_managed_local_events_task(
                events_task,
                timeout_secs=MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS,
            )

        if not new_events and terminal_result is None:
            if not events_task.done():
                new_events = await events_task
            else:
                new_events = events_task.result() or []
    finally:
        if terminal_result is None and terminal_task.done():
            try:
                terminal_result = terminal_task.result()
            except Exception:
                terminal_result = None
        for task in (terminal_task, events_task):
            if task.done():
                continue
            task.cancel()
        await asyncio.gather(terminal_task, events_task, return_exceptions=True)

    if terminal_result is not None:
        run_best_effort_managed_local_turn_write(
            db_bind=db.get_bind(),
            label="terminal",
            fn=lambda turn_db: mark_managed_local_turn_terminal(
                turn_db,
                session_id=source_session.id,
                request_id=request_id,
                phase=terminal_result.phase,
                terminal_at=terminal_result.occurred_at,
                terminal_runtime_event_id=terminal_result.runtime_event_id,
            ),
        )
    if new_events:
        run_best_effort_managed_local_turn_write(
            db_bind=db.get_bind(),
            label="durable",
            fn=lambda turn_db: maybe_mark_managed_local_turn_durable(
                turn_db,
                session_id=source_session.id,
            ),
        )
    turn_snapshot = _get_managed_local_turn_snapshot_best_effort(
        db_bind=db.get_bind(),
        session_id=source_session.id,
        request_id=request_id,
    )
    if not new_events:
        ledger_snapshot, ledger_events = _hydrate_managed_local_turn_events_from_ledger(
            db_bind=db.get_bind(),
            session_id=source_session.id,
            request_id=request_id,
            expected_user_message=message,
        )
        if ledger_snapshot is not None:
            turn_snapshot = ledger_snapshot
        if ledger_events:
            new_events = ledger_events

    if not new_events and terminal_result is None and not (turn_snapshot and turn_snapshot.terminal_at is not None):
        run_best_effort_managed_local_turn_write(
            db_bind=db.get_bind(),
            label="turn_timeout",
            fn=lambda turn_db: mark_managed_local_turn_failed(
                turn_db,
                session_id=source_session.id,
                request_id=request_id,
                error_code="turn_timeout",
            ),
        )
        persistence_error = _MANAGED_LOCAL_TURN_TIMEOUT_MESSAGE
        yield SSEEvent(
            event="error",
            data=json.dumps({"error": persistence_error}),
        ).encode()
        yield SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": str(source_session.id),
                    "source_session_id": str(source_session.id),
                    "shipped_session_id": None,
                    "created_branch": False,
                    "control_status": MANAGED_LOCAL_CONTROL_STATUS_FAILED,
                    "sync_status": MANAGED_LOCAL_SYNC_STATUS_FAILED,
                    "exit_code": 0,
                    "total_text_length": 0,
                    "persisted_events": 0,
                    "persistence_error": persistence_error,
                    "sync_note": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()
        return

    if turn_snapshot is not None and turn_snapshot.terminal_at is not None:
        control_status = get_managed_local_control_status_for_phase(turn_snapshot.terminal_phase)
    elif terminal_result is None:
        control_status = MANAGED_LOCAL_CONTROL_STATUS_COMPLETED
    else:
        control_status = terminal_result.control_status

    turn_reached_terminal = (turn_snapshot is not None and turn_snapshot.terminal_at is not None) or terminal_result is not None
    if not new_events and turn_reached_terminal:
        yield SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": str(source_session.id),
                    "source_session_id": str(source_session.id),
                    "shipped_session_id": str(source_session.id),
                    "created_branch": False,
                    "control_status": control_status,
                    "sync_status": MANAGED_LOCAL_SYNC_STATUS_PENDING,
                    "exit_code": 0,
                    "total_text_length": 0,
                    "persisted_events": 0,
                    "persistence_error": None,
                    "sync_note": _MANAGED_LOCAL_SYNC_PENDING_NOTE,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()
        return

    assistant_text = ""
    for event in new_events:
        if event.tool_name:
            yield SSEEvent(
                event="tool_use",
                data=json.dumps(
                    {
                        "name": event.tool_name,
                        "id": event.tool_call_id or str(event.id),
                    }
                ),
            ).encode()
        if event.role == "assistant" and event.content_text:
            assistant_text += event.content_text
            yield SSEEvent(
                event="assistant_delta",
                data=json.dumps({"text": event.content_text, "accumulated": assistant_text}),
            ).encode()

    yield SSEEvent(
        event="done",
        data=json.dumps(
            {
                "session_id": str(source_session.id),
                "source_session_id": str(source_session.id),
                "shipped_session_id": str(source_session.id),
                "created_branch": False,
                "control_status": control_status,
                "sync_status": MANAGED_LOCAL_SYNC_STATUS_COMPLETE,
                "exit_code": 0,
                "total_text_length": len(assistant_text),
                "persisted_events": len(new_events),
                "persistence_error": None,
                "sync_note": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ),
    ).encode()


# ---------------------------------------------------------------------------
# SSE Event Types
# ---------------------------------------------------------------------------


@dataclass
class SSEEvent:
    """Server-sent event."""

    event: str
    data: str

    def encode(self) -> str:
        """Encode as SSE format."""
        return f"event: {self.event}\ndata: {self.data}\n\n"


async def _stream_fake_session_cloud_branch_output(
    *,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    created_branch: bool,
    branched_from_event_id: int | None,
    workspace_path: Path,
    message: str,
    db: Session | None = None,
) -> AsyncIterator[str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    assistant_text = f"Test cloud-branch reply to: {message}"

    yield SSEEvent(
        event="system",
        data=json.dumps(
            {
                "type": "session_started",
                "session_id": target_session_id,
                "source_session_id": source_session_id,
                "thread_root_session_id": thread_root_session_id,
                "continued_from_session_id": continued_from_session_id,
                "created_branch": created_branch,
                "workspace": str(workspace_path),
                "timestamp": timestamp,
            }
        ),
    ).encode()
    yield SSEEvent(
        event="assistant_delta",
        data=json.dumps({"text": assistant_text, "accumulated": assistant_text}),
    ).encode()

    ship_result: ShipSessionResult | None
    persistence_error: str | None = None
    try:
        ship_result = _persist_cloud_branch_turn(
            db=db,
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            thread_root_session_id=thread_root_session_id,
            continued_from_session_id=continued_from_session_id,
            branched_from_event_id=branched_from_event_id,
            workspace_path=workspace_path,
            message=message,
            assistant_text=assistant_text,
        )
        if db is not None:
            db.commit()
    except Exception as exc:
        if db is not None:
            db.rollback()
        logger.warning("Failed to persist fake cloud-branch turn for %s: %s", target_session_id, exc)
        ship_result = None
        persistence_error = _CLOUD_BRANCH_PERSISTENCE_ERROR

    if ship_result is not None and ship_result.events_inserted <= 0 and persistence_error is None:
        persistence_error = _CLOUD_BRANCH_EMPTY_EVENTS_ERROR

    yield SSEEvent(
        event="done",
        data=json.dumps(
            {
                "session_id": target_session_id,
                "source_session_id": source_session_id,
                "shipped_session_id": ship_result.session_id if ship_result else None,
                "created_branch": created_branch,
                "branched_from_event_id": branched_from_event_id,
                "exit_code": 0,
                "total_text_length": len(assistant_text),
                "persisted_events": ship_result.events_inserted if ship_result else 0,
                "persistence_error": persistence_error,
                "timestamp": timestamp,
            }
        ),
    ).encode()


def _persist_cloud_branch_turn(
    *,
    db: Session | None,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    branched_from_event_id: int | None,
    workspace_path: Path,
    message: str,
    assistant_text: str,
) -> ShipSessionResult:
    if db is None:
        return ShipSessionResult(
            session_id=target_session_id,
            events_inserted=2,
            events_skipped=0,
            session_created=False,
        )

    from zerg.services.agents_store import AgentsStore
    from zerg.services.agents_store import EventIngest
    from zerg.services.agents_store import SessionIngest
    from zerg.services.agents_store import SourceLineIngest

    store = AgentsStore(db)
    target_uuid = UUID(target_session_id)
    source_uuid = UUID(source_session_id)
    target_session = store.get_session(target_uuid)
    source_session = store.get_session(source_uuid)

    user_timestamp = datetime.now(timezone.utc)
    assistant_timestamp = user_timestamp + timedelta(milliseconds=1)
    source_path = f"/tmp/cloud-branch-{target_session_id}-{int(user_timestamp.timestamp() * 1000)}.jsonl"
    user_raw = json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": message}]},
            "timestamp": user_timestamp.isoformat(),
        }
    )
    assistant_raw = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": assistant_text}]},
            "timestamp": assistant_timestamp.isoformat(),
        }
    )
    if target_session:
        resolved_git_branch = target_session.git_branch
        resolved_thread_root_session_id = target_session.thread_root_session_id
    else:
        resolved_git_branch = source_session.git_branch if source_session else None
        resolved_thread_root_session_id = UUID(thread_root_session_id)
    resolved_provider_session_id = target_session_id
    if target_session and str(target_session.provider_session_id or "").strip():
        existing_provider_session_id = str(target_session.provider_session_id).strip()
        source_provider_session_id = str(source_session.provider_session_id or "").strip() if source_session else ""
        if existing_provider_session_id != source_provider_session_id:
            resolved_provider_session_id = existing_provider_session_id

    payload = SessionIngest(
        id=target_uuid,
        provider=(target_session.provider if target_session else "claude"),
        environment=(
            target_session.environment
            if target_session and target_session.environment
            else (source_session.environment if source_session and source_session.environment else "Cloud")
        ),
        project=target_session.project if target_session else (source_session.project if source_session else None),
        device_id=(
            target_session.device_id
            if target_session and target_session.device_id
            else (source_session.device_id if source_session else "zerg-commis-cloud")
        ),
        cwd=target_session.cwd if target_session else str(workspace_path.absolute()),
        git_repo=target_session.git_repo if target_session else (source_session.git_repo if source_session else None),
        git_branch=resolved_git_branch,
        started_at=target_session.started_at if target_session else user_timestamp,
        ended_at=assistant_timestamp,
        provider_session_id=resolved_provider_session_id,
        thread_root_session_id=resolved_thread_root_session_id,
        continued_from_session_id=(
            target_session.continued_from_session_id
            if target_session and target_session.continued_from_session_id
            else (UUID(continued_from_session_id) if continued_from_session_id else None)
        ),
        continuation_kind="cloud",
        origin_label="Cloud",
        branched_from_event_id=(
            target_session.branched_from_event_id
            if target_session and target_session.branched_from_event_id is not None
            else branched_from_event_id
        ),
        is_sidechain=bool(target_session.is_sidechain) if target_session else False,
        events=[
            EventIngest(
                role="user",
                content_text=message,
                timestamp=user_timestamp,
                source_path=source_path,
                source_offset=0,
                raw_json=user_raw,
            ),
            EventIngest(
                role="assistant",
                content_text=assistant_text,
                timestamp=assistant_timestamp,
                source_path=source_path,
                source_offset=1,
                raw_json=assistant_raw,
            ),
        ],
        source_lines=[
            SourceLineIngest(
                source_path=source_path,
                source_offset=0,
                raw_json=user_raw,
            ),
            SourceLineIngest(
                source_path=source_path,
                source_offset=1,
                raw_json=assistant_raw,
            ),
        ],
    )
    result = store.ingest_session(payload)
    return ShipSessionResult(
        session_id=str(result.session_id),
        events_inserted=result.events_inserted,
        events_skipped=result.events_skipped,
        session_created=result.session_created,
    )


async def stream_session_cloud_branch_output(
    *,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    created_branch: bool,
    branched_from_event_id: int | None,
    workspace_path: Path,
    message: str,
    prompt: str,
    request_id: str,
    db: Session | None = None,
) -> AsyncIterator[str]:
    """Stream explicit cloud-branch output as SSE events.

    Yields SSE events:
    - system: Session info, status updates
    - assistant_delta: Streaming text from the provider
    - tool_use: Tool calls
    - error: Error messages
    - done: Completion signal
    """
    proc = None
    try:
        if _truthy_env("TESTING") and _truthy_env("E2E_FAKE_SESSION_CHAT"):
            async for event in _stream_fake_session_cloud_branch_output(
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                thread_root_session_id=thread_root_session_id,
                continued_from_session_id=continued_from_session_id,
                created_branch=created_branch,
                branched_from_event_id=branched_from_event_id,
                workspace_path=workspace_path,
                message=message,
                db=db,
            ):
                yield event
            return

        runtime = _build_claude_branch_runtime(prompt=prompt)

        yield SSEEvent(
            event="system",
            data=json.dumps(
                {
                    "type": "session_started",
                    "session_id": target_session_id,
                    "source_session_id": source_session_id,
                    "thread_root_session_id": thread_root_session_id,
                    "continued_from_session_id": continued_from_session_id,
                    "created_branch": created_branch,
                    "workspace": str(workspace_path),
                    "execution_backend": runtime.backend,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()

        proc_env = os.environ.copy()
        proc_env.update(runtime.env_updates)
        for env_name in runtime.env_unset:
            proc_env.pop(env_name, None)

        logger.info(
            "[%s] Starting %s cloud branch: backend=%s cwd=%s",
            request_id,
            "claude",
            runtime.backend,
            workspace_path,
        )

        proc = await asyncio.create_subprocess_exec(
            *runtime.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=workspace_path,
            env=proc_env,
        )

        assistant_text = ""
        async for line in proc.stdout:
            line = line.decode().strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                event_type = event.get("type", "unknown")

                if event_type == "assistant":
                    msg = event.get("message", {})
                    content = msg.get("content", [])
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_content = block.get("text", "")
                                assistant_text += text_content
                                yield SSEEvent(
                                    event="assistant_delta",
                                    data=json.dumps(
                                        {
                                            "text": text_content,
                                            "accumulated": assistant_text,
                                        }
                                    ),
                                ).encode()
                            elif block.get("type") == "tool_use":
                                yield SSEEvent(
                                    event="tool_use",
                                    data=json.dumps(
                                        {
                                            "name": block.get("name"),
                                            "id": block.get("id"),
                                        }
                                    ),
                                ).encode()

                elif event_type == "result":
                    yield SSEEvent(
                        event="tool_result",
                        data=json.dumps(
                            {
                                "result": str(event.get("result", ""))[:500],
                            }
                        ),
                    ).encode()

                elif event_type == "system":
                    yield SSEEvent(
                        event="system",
                        data=json.dumps(
                            {
                                "type": "provider_system",
                                "session_id": event.get("session_id"),
                            }
                        ),
                    ).encode()

            except json.JSONDecodeError:
                logger.debug(f"[{request_id}] Non-JSON output: {line[:100]}")

        await proc.wait()

        shipped_id: str | None = None
        persisted_events = 0
        persistence_error: str | None = None
        if proc.returncode != 0:
            logger.error(f"[{request_id}] Claude exited with code {proc.returncode}")
            yield SSEEvent(
                event="error",
                data=json.dumps(
                    {
                        "error": f"Claude exited with code {proc.returncode}",
                        "details": "Process exited with non-zero status",
                    }
                ),
            ).encode()
        else:
            try:
                ship_result = await ship_session_to_zerg(
                    workspace_path=workspace_path,
                    commis_id=request_id,
                    db=db,
                    session_id=target_session_id,
                    thread_root_session_id=thread_root_session_id,
                    continued_from_session_id=continued_from_session_id,
                    continuation_kind="cloud",
                    origin_label="Cloud",
                    branched_from_event_id=branched_from_event_id,
                    provider="claude",
                )
                if ship_result:
                    shipped_id = ship_result.session_id
                    persisted_events = ship_result.events_inserted
                    if ship_result.events_inserted > 0:
                        logger.info(
                            "[%s] Shipped session to Longhouse: %s (events inserted=%s skipped=%s)",
                            request_id,
                            shipped_id,
                            ship_result.events_inserted,
                            ship_result.events_skipped,
                        )
                    else:
                        persistence_error = (
                            "Response completed, but Longhouse could not extract any new timeline events "
                            "from the cloud branch transcript."
                        )
                        logger.warning("[%s] Shipped cloud branch contained no new events", request_id)
                else:
                    persistence_error = _CLOUD_BRANCH_PERSISTENCE_ERROR
            except Exception as e:
                persistence_error = _CLOUD_BRANCH_PERSISTENCE_ERROR
                logger.warning(f"[{request_id}] Failed to ship session to Longhouse: {e}")

        yield SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": target_session_id,
                    "source_session_id": source_session_id,
                    "shipped_session_id": shipped_id,
                    "created_branch": created_branch,
                    "branched_from_event_id": branched_from_event_id,
                    "exit_code": proc.returncode,
                    "execution_backend": runtime.backend if "runtime" in locals() else SESSION_CHAT_BACKEND_AMBIENT,
                    "total_text_length": len(assistant_text),
                    "persisted_events": persisted_events,
                    "persistence_error": persistence_error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()

    except asyncio.CancelledError:
        logger.info(f"[{request_id}] Request cancelled by client")
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        yield SSEEvent(
            event="error",
            data=json.dumps({"error": "Request cancelled"}),
        ).encode()
        raise

    except Exception as e:
        logger.exception(f"[{request_id}] Error streaming cloud branch output")
        yield SSEEvent(
            event="error",
            data=json.dumps({"error": str(e)[:500]}),
        ).encode()

    finally:
        if proc and proc.returncode is None:
            proc.terminate()


@router.post("/{session_id}/branch-cloud")
async def branch_session_in_cloud(
    session_id: str,
    body: SessionMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_oikos_user),
):
    """Start an explicit cloud branch and stream the response via SSE."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    logger.info(f"[{request_id}] Cloud branch request for session {source_session.id}")
    _assert_cloud_branch_available(source_session)
    lock_scope_id = await _acquire_session_lock_or_raise(source_session=source_session, request_id=request_id)
    try:
        return await _build_cloud_branch_response(
            source_session=source_session,
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
        logger.exception(f"[{request_id}] Error in branch_session_in_cloud")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc


@router.post("/{session_id}/send-live")
async def send_to_live_session(
    session_id: str,
    body: SessionMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_oikos_user),
):
    """Send text into the live managed-local session and return a fast JSON ack."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    logger.info(f"[{request_id}] Live session send request for session {source_session.id}")
    _assert_live_session_send_available(source_session)
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


@agents_router.post("/{session_id}/branch-cloud")
async def branch_session_in_cloud_agents(
    session_id: str,
    body: SessionMessageRequest,
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
):
    """Start a cloud branch through the canonical machine-facing agents surface."""
    settings = get_settings()
    resolved_device_token = device_token if isinstance(device_token, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _authorize_cloud_branch(
        request=request,
        device_token=resolved_device_token,
        source_session=source_session,
        auth_disabled=settings.auth_disabled,
    )
    _assert_cloud_branch_available(source_session)
    lock_scope_id = await _acquire_session_lock_or_raise(source_session=source_session, request_id=request_id)

    try:
        return await _build_cloud_branch_response(
            source_session=source_session,
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
        logger.exception(f"[{request_id}] Error in branch_session_in_cloud_agents")
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
    _assert_live_session_send_available(source_session)
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
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


@router.post("/managed-local", response_model=ManagedLocalSessionLaunchResponse)
async def launch_managed_local(
    body: ManagedLocalSessionLaunchRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_oikos_user),
):
    """Start a managed local AI agent session on a connected runner.

    Supports both Claude and Codex providers under a transport-aware contract.
    """
    hook_url = get_request_public_base_url(request)
    try:
        result = await launch_managed_local_session(
            db,
            ManagedLocalLaunchParams(
                owner_id=current_user.id,
                runner_target=body.runner_target,
                cwd=body.cwd,
                provider=body.provider,
                project=body.project,
                git_repo=body.git_repo,
                git_branch=body.git_branch,
                display_name=body.display_name,
                loop_mode=body.loop_mode.value,
                hook_url=hook_url,
                claude_launch_env=body.claude_launch_env,
            ),
        )
    except ManagedLocalLaunchError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception:
        db.rollback()
        logger.exception("Managed local launch failed unexpectedly")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Managed local launch failed")

    return _managed_local_launch_response(result)


@router.post("/managed-local/this-device", response_model=ManagedLocalSessionLaunchResponse)
async def launch_managed_local_this_device(
    body: ManagedLocalThisDeviceLaunchRequest,
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
):
    """Start a managed local AI agent session on the calling machine's connected runner."""

    owner_id = _resolve_agents_owner_id(db, device_token)
    machine_name = (body.machine_name or "").strip() or str(getattr(device_token, "device_id", "") or "").strip()
    if not machine_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not determine this device name")
    hook_url = get_request_public_base_url(request)

    try:
        result = await launch_managed_local_session(
            db,
            ManagedLocalLaunchParams(
                owner_id=owner_id,
                runner_target=machine_name,
                cwd=body.cwd,
                provider=body.provider,
                project=body.project,
                git_repo=body.git_repo,
                git_branch=body.git_branch,
                display_name=body.display_name,
                loop_mode=body.loop_mode.value,
                hook_url=hook_url,
                machine_name=machine_name,
                native_claude_channels_available=body.native_claude_channels_available,
                claude_launch_env=body.claude_launch_env,
            ),
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

    return _managed_local_launch_response(result)


@router.get("/{session_id}/lock")
async def get_session_lock_status(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_oikos_user),
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


@router.delete("/{session_id}/lock")
async def force_release_lock(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_oikos_user),
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
