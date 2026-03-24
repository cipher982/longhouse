"""Session chat router for live-session drop-in functionality.

Enables interactive chat with Claude Code sessions via turn-by-turn resume.
Each message spawns: claude --resume {id} -p "message" --output-format stream-json

Security:
- Workspace path derived server-side from session metadata (not client)
- Per-session locks prevent concurrent resumes
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import AgentEvent
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.request_urls import get_request_public_base_url
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_local_control import MANAGED_LOCAL_CONTROL_STATUS_COMPLETED
from zerg.services.managed_local_control import MANAGED_LOCAL_CONTROL_STATUS_FAILED
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_COMPLETE
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_FAILED
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_PENDING
from zerg.services.managed_local_control import await_managed_local_turn_terminal
from zerg.services.managed_local_control import get_managed_local_latest_hook_runtime_event_id
from zerg.services.managed_local_control import get_managed_local_presence_updated_at
from zerg.services.managed_local_control import send_text_to_managed_local_session
from zerg.services.managed_local_control import ship_managed_local_claude_transcript
from zerg.services.managed_local_launcher import ManagedLocalLaunchError
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import launch_managed_local_session
from zerg.services.session_continuity import ShipSessionResult
from zerg.services.session_continuity import prepare_session_for_resume
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_continuity import ship_session_to_zerg
from zerg.services.session_continuity import workspace_resolver
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_loop_mode import SessionLoopMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-chat"])

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
# Anthropic-ecosystem model defaults for session continuation (not in models.json —
# these are Claude Code resume models, not OpenAI-compatible chat models).
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
MANAGED_LOCAL_POLL_INTERVAL_SECS = 1.0
MANAGED_LOCAL_STABLE_POLLS = 1
MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS = 0.5
MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS = 10.0
MANAGED_LOCAL_POST_FORCE_SYNC_GRACE_SECS = 1.0
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
_CONTINUATION_PERSISTENCE_ERROR = "".join(
    [
        "Response completed, but Longhouse could not save the ",
        "continuation transcript to the timeline.",
    ]
)
_CONTINUATION_EMPTY_EVENTS_ERROR = "".join(
    [
        "Response completed, but Longhouse could not extract any new ",
        "timeline events from the continuation transcript.",
    ]
)


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
class ClaudeResumeRuntime:
    backend: str
    cmd: list[str]
    env_updates: dict[str, str]
    env_unset: tuple[str, ...] = ()


def _check_claude_binary() -> bool:
    """Check if the claude CLI binary is available on PATH."""
    import shutil

    return shutil.which("claude") is not None


def _build_claude_resume_runtime(*, provider_session_id: str, message: str) -> ClaudeResumeRuntime:
    cmd = [
        "claude",
        "--resume",
        provider_session_id,
        "-p",
        message,
        "--output-format",
        "stream-json",
        "--verbose",
        "--print",
    ]
    backend = _get_session_chat_backend()
    if not _check_claude_binary():
        raise RuntimeError(
            "Session continuation requires the 'claude' CLI but it is not installed. "
            "Install @anthropic-ai/claude-code (npm install -g @anthropic-ai/claude-code)."
        )

    if backend == SESSION_CHAT_BACKEND_AMBIENT:
        return ClaudeResumeRuntime(backend=backend, cmd=cmd, env_updates={})

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
        return ClaudeResumeRuntime(
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
        return ClaudeResumeRuntime(
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
    return ClaudeResumeRuntime(
        backend=backend,
        cmd=cmd,
        env_updates=env_updates,
        env_unset=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class SessionChatRequest(BaseModel):
    """Request to chat with a session."""

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
    managed_transport: ManagedSessionTransport = Field(
        ManagedSessionTransport.TMUX,
        description="Managed local transport (tmux only in v1)",
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


class ManagedLocalSessionLaunchResponse(BaseModel):
    """Response after successfully starting a managed local session."""

    session_id: str
    provider: str
    provider_session_id: str
    execution_home: SessionExecutionHome
    managed_transport: ManagedSessionTransport
    loop_mode: SessionLoopMode
    source_runner_id: int
    source_runner_name: str
    managed_session_name: str
    attach_command: str


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
    return ManagedLocalSessionLaunchResponse(
        session_id=str(session.id),
        provider=session.provider or "claude",
        provider_session_id=session.provider_session_id or str(session.id),
        execution_home=SessionExecutionHome(session.execution_home or SessionExecutionHome.LEGACY.value),
        managed_transport=ManagedSessionTransport(session.managed_transport or ManagedSessionTransport.TMUX.value),
        loop_mode=SessionLoopMode(session.loop_mode or SessionLoopMode.MANUAL.value),
        source_runner_id=int(session.source_runner_id or 0),
        source_runner_name=session.source_runner_name or "",
        managed_session_name=session.managed_session_name or "",
        attach_command=result.attach_command,
    )


def _lock_scope_id_for_session(db: Session, session_id: str) -> str:
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return session_id
    session = AgentsStore(db).get_session(session_uuid)
    if session is None:
        return session_id
    return str(session.thread_root_session_id or session.id)


def _fetch_managed_local_events_since(*, db_bind, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id > after_event_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


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
        if role == "user" and content_text == expected_user_message:
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

    while time.monotonic() < deadline:
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

        await asyncio.sleep(poll_interval_secs)

    return []


async def _force_managed_local_claude_sync(
    *,
    db_bind,
    owner_id: int,
    source_session,
    request_id: str,
):
    with Session(bind=db_bind) as ship_db:
        return await ship_managed_local_claude_transcript(
            db=ship_db,
            owner_id=owner_id,
            session=source_session,
            commis_id=request_id,
        )


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
    if not source_session.source_runner_id or not source_session.managed_session_name:
        raise RuntimeError("Managed local session is missing runner or tmux metadata")

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
                "created_continuation": False,
                "provider_session_id": source_session.provider_session_id,
                "execution_home": source_session.execution_home,
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
    send_result = await send_text_to_managed_local_session(
        db=db,
        owner_id=owner_id,
        session=source_session,
        text=message,
        commis_id=request_id,
        timeout_secs=15,
    )

    if not send_result.ok:
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
                    "created_continuation": False,
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
        done, _pending = await asyncio.wait(
            {terminal_task, events_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if events_task in done:
            new_events = events_task.result() or []

        if not new_events:
            if terminal_task in done:
                terminal_result = terminal_task.result()
            elif not terminal_task.done():
                terminal_result = await terminal_task

        if not new_events and terminal_result is not None:
            provider_is_claude = str(getattr(source_session, "provider", "") or "").strip().lower() == "claude"
            if provider_is_claude:
                initial_grace_secs = MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS
            else:
                initial_grace_secs = MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS
            new_events = await _await_managed_local_events_task(
                events_task,
                timeout_secs=initial_grace_secs,
            )

            ship_result = None
            if not new_events and provider_is_claude:
                ship_task = asyncio.create_task(
                    _force_managed_local_claude_sync(
                        db_bind=db.get_bind(),
                        owner_id=owner_id,
                        source_session=source_session,
                        request_id=request_id,
                    )
                )
                try:
                    done, _ = await asyncio.wait(
                        {events_task, ship_task},
                        timeout=MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if events_task in done:
                        new_events = events_task.result() or []
                    elif ship_task in done:
                        ship_result = ship_task.result()
                        new_events = await _await_managed_local_events_task(
                            events_task,
                            timeout_secs=MANAGED_LOCAL_POST_FORCE_SYNC_GRACE_SECS,
                        )
                finally:
                    if not ship_task.done():
                        ship_task.cancel()
                        await asyncio.gather(ship_task, return_exceptions=True)

            if ship_result is not None and not ship_result.ok:
                log_message = ship_result.error or f"exit_code={ship_result.exit_code}"
                if new_events:
                    logger.debug(
                        "Managed-local Claude direct ship found no extra events for %s: %s",
                        source_session.id,
                        log_message,
                    )
                else:
                    logger.warning(
                        "Managed-local Claude direct ship failed for %s: %s",
                        source_session.id,
                        log_message,
                    )

        if not new_events and terminal_result is None:
            if not events_task.done():
                new_events = await events_task
            else:
                new_events = events_task.result() or []
    finally:
        for task in (terminal_task, events_task):
            if task.done():
                continue
            task.cancel()
        await asyncio.gather(terminal_task, events_task, return_exceptions=True)

    if not new_events and terminal_result is None:
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
                    "created_continuation": False,
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

    if terminal_result is None:
        control_status = MANAGED_LOCAL_CONTROL_STATUS_COMPLETED
    else:
        control_status = terminal_result.control_status

    if not new_events and terminal_result is not None:
        yield SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": str(source_session.id),
                    "source_session_id": str(source_session.id),
                    "shipped_session_id": str(source_session.id),
                    "created_continuation": False,
                    "control_status": terminal_result.control_status,
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
                "created_continuation": False,
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


async def _stream_fake_claude_output(
    *,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    created_continuation: bool,
    branched_from_event_id: int | None,
    provider_session_id: str,
    workspace_path: Path,
    message: str,
    db: Session | None = None,
) -> AsyncIterator[str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    assistant_text = f"Test continuation reply to: {message}"

    yield SSEEvent(
        event="system",
        data=json.dumps(
            {
                "type": "session_started",
                "session_id": target_session_id,
                "source_session_id": source_session_id,
                "thread_root_session_id": thread_root_session_id,
                "continued_from_session_id": continued_from_session_id,
                "created_continuation": created_continuation,
                "provider_session_id": provider_session_id,
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
        ship_result = _persist_fake_continuation_turn(
            db=db,
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            thread_root_session_id=thread_root_session_id,
            continued_from_session_id=continued_from_session_id,
            branched_from_event_id=branched_from_event_id,
            provider_session_id=provider_session_id,
            workspace_path=workspace_path,
            message=message,
            assistant_text=assistant_text,
        )
    except Exception as exc:
        logger.warning("Failed to persist fake continuation turn for %s: %s", target_session_id, exc)
        ship_result = None
        persistence_error = _CONTINUATION_PERSISTENCE_ERROR

    if ship_result is not None and ship_result.events_inserted <= 0 and persistence_error is None:
        persistence_error = _CONTINUATION_EMPTY_EVENTS_ERROR

    yield SSEEvent(
        event="done",
        data=json.dumps(
            {
                "session_id": target_session_id,
                "source_session_id": source_session_id,
                "shipped_session_id": ship_result.session_id if ship_result else None,
                "created_continuation": created_continuation,
                "branched_from_event_id": branched_from_event_id,
                "exit_code": 0,
                "total_text_length": len(assistant_text),
                "persisted_events": ship_result.events_inserted if ship_result else 0,
                "persistence_error": persistence_error,
                "timestamp": timestamp,
            }
        ),
    ).encode()


def _persist_fake_continuation_turn(
    *,
    db: Session | None,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    branched_from_event_id: int | None,
    provider_session_id: str,
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
    source_path = f"/tmp/fake-continuation-{target_session_id}-{int(user_timestamp.timestamp() * 1000)}.jsonl"
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
        provider_session_id=provider_session_id,
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


async def stream_claude_output(
    *,
    source_session_id: str,
    target_session_id: str,
    thread_root_session_id: str,
    continued_from_session_id: str | None,
    created_continuation: bool,
    branched_from_event_id: int | None,
    provider_session_id: str,
    workspace_path: Path,
    message: str,
    request_id: str,
    db: Session | None = None,
) -> AsyncIterator[str]:
    """Stream Claude Code output as SSE events.

    Yields SSE events:
    - system: Session info, status updates
    - assistant_delta: Streaming text from Claude
    - tool_use: Tool calls
    - error: Error messages
    - done: Completion signal
    """
    proc = None
    try:
        if _truthy_env("TESTING") and _truthy_env("E2E_FAKE_SESSION_CHAT"):
            async for event in _stream_fake_claude_output(
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                thread_root_session_id=thread_root_session_id,
                continued_from_session_id=continued_from_session_id,
                created_continuation=created_continuation,
                branched_from_event_id=branched_from_event_id,
                provider_session_id=provider_session_id,
                workspace_path=workspace_path,
                message=message,
                db=db,
            ):
                yield event
            return

        runtime = _build_claude_resume_runtime(provider_session_id=provider_session_id, message=message)

        yield SSEEvent(
            event="system",
            data=json.dumps(
                {
                    "type": "session_started",
                    "session_id": target_session_id,
                    "source_session_id": source_session_id,
                    "thread_root_session_id": thread_root_session_id,
                    "continued_from_session_id": continued_from_session_id,
                    "created_continuation": created_continuation,
                    "provider_session_id": provider_session_id,
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
            "[%s] Starting Claude-compatible continuation: backend=%s cwd=%s",
            request_id,
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
                                "type": "claude_system",
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
                            "from the continuation transcript."
                        )
                        logger.warning("[%s] Shipped continuation contained no new events", request_id)
                else:
                    persistence_error = _CONTINUATION_PERSISTENCE_ERROR
            except Exception as e:
                persistence_error = _CONTINUATION_PERSISTENCE_ERROR
                logger.warning(f"[{request_id}] Failed to ship session to Longhouse: {e}")

        yield SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": target_session_id,
                    "source_session_id": source_session_id,
                    "shipped_session_id": shipped_id,
                    "created_continuation": created_continuation,
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
        logger.exception(f"[{request_id}] Error streaming Claude output")
        yield SSEEvent(
            event="error",
            data=json.dumps({"error": str(e)[:500]}),
        ).encode()

    finally:
        if proc and proc.returncode is None:
            proc.terminate()


@router.post("/{session_id}/chat")
async def chat_with_session(
    session_id: str,
    body: SessionChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_oikos_user),
):
    """Chat with a Claude Code session.

    Resumes an existing session and streams the response via SSE.
    """
    request_id = str(uuid.uuid4())[:8]
    logger.info(f"[{request_id}] Chat request for session {session_id}")

    try:
        source_session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid session id: {session_id}",
        ) from exc

    store = AgentsStore(db)
    source_session = store.get_session(source_session_uuid)
    if not source_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    if source_session.provider not in ("claude", "codex"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only Claude and Codex sessions can be resumed (got {source_session.provider})",
        )

    # Non-managed-local continuation requires Claude (spawns claude subprocess)
    is_non_claude_resume = source_session.provider != "claude"
    is_non_managed_local_resume = source_session.execution_home != SessionExecutionHome.MANAGED_LOCAL.value
    if is_non_claude_resume and is_non_managed_local_resume:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Non-managed-local continuation is only supported for Claude (got {source_session.provider})",
        )

    lock_scope_id = str(source_session.thread_root_session_id or source_session.id)
    lock = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder=request_id,
        ttl_seconds=300,
    )

    if not lock:
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

    resolved_workspace = None

    try:
        if source_session.execution_home == SessionExecutionHome.MANAGED_LOCAL.value:
            if source_session.managed_transport != ManagedSessionTransport.TMUX.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unsupported managed local transport: {source_session.managed_transport}",
                )

            async def generate_managed_local():
                try:
                    async for event in _stream_managed_local_output(
                        source_session=source_session,
                        owner_id=current_user.id,
                        message=body.message,
                        request_id=request_id,
                        db=db,
                    ):
                        yield event
                finally:
                    await session_lock_manager.release(lock_scope_id, request_id)
                    logger.info(f"[{request_id}] Managed local chat complete, lock released")

            return StreamingResponse(
                generate_managed_local(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        target_session, created_continuation = store.ensure_cloud_continuation_target(source_session.id)
        db.commit()

        resolved_workspace = await workspace_resolver.resolve(
            original_cwd=source_session.cwd,
            git_repo=source_session.git_repo,
            git_branch=source_session.git_branch,
            session_id=str(target_session.id),
        )

        if resolved_workspace.error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resolve workspace: {resolved_workspace.error}",
            )

        provider_session_id = await prepare_session_for_resume(
            session_id=str(source_session.id),
            workspace_path=resolved_workspace.path,
            db=db,
        )

        logger.info(
            f"[{request_id}] Prepared source session {source_session.id} -> {provider_session_id[:20]}... "
            f"target={target_session.id} workspace={resolved_workspace.path} is_temp={resolved_workspace.is_temp}"
        )

        async def generate():
            try:
                continued_from_session_id = (
                    str(target_session.continued_from_session_id) if target_session.continued_from_session_id else None
                )
                async for event in stream_claude_output(
                    source_session_id=str(source_session.id),
                    target_session_id=str(target_session.id),
                    thread_root_session_id=str(target_session.thread_root_session_id or target_session.id),
                    continued_from_session_id=continued_from_session_id,
                    created_continuation=created_continuation,
                    branched_from_event_id=target_session.branched_from_event_id,
                    provider_session_id=provider_session_id,
                    workspace_path=resolved_workspace.path,
                    message=body.message,
                    request_id=request_id,
                    db=db,
                ):
                    yield event
            finally:
                await session_lock_manager.release(lock_scope_id, request_id)
                if resolved_workspace and resolved_workspace.is_temp:
                    resolved_workspace.cleanup()
                logger.info(f"[{request_id}] Session chat complete, lock released")

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except HTTPException:
        await session_lock_manager.release(lock_scope_id, request_id)
        if resolved_workspace and resolved_workspace.is_temp:
            resolved_workspace.cleanup()
        raise

    except Exception as e:
        await session_lock_manager.release(lock_scope_id, request_id)
        if resolved_workspace and resolved_workspace.is_temp:
            resolved_workspace.cleanup()
        logger.exception(f"[{request_id}] Error in chat_with_session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(e)[:200]}",
        )


@router.post("/managed-local", response_model=ManagedLocalSessionLaunchResponse)
async def launch_managed_local(
    body: ManagedLocalSessionLaunchRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_oikos_user),
):
    """Start a managed local AI agent session inside tmux on a connected runner.

    Supports both Claude and Codex providers. The tmux transport is
    provider-agnostic — Longhouse owns the launch, lifecycle, and input routing.
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
                managed_transport=body.managed_transport.value,
                hook_url=hook_url,
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


@router.get("/continuation-readiness")
async def continuation_readiness(
    _current_user=Depends(get_current_oikos_user),
) -> dict:
    """Pre-flight check: can this instance run session continuations?

    Returns backend config and whether the required binary/keys are present.
    Used by QA and the frontend to show actionable errors.
    """
    backend = _get_session_chat_backend()
    issues: list[str] = []

    if not _check_claude_binary():
        issues.append("'claude' CLI not found on PATH (required for all backends)")

    if backend == SESSION_CHAT_BACKEND_ZAI:
        if not os.getenv("ZAI_API_KEY", "").strip():
            issues.append("ZAI_API_KEY not set")
    elif backend == SESSION_CHAT_BACKEND_ANTHROPIC:
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            issues.append("ANTHROPIC_API_KEY not set")
    elif backend == SESSION_CHAT_BACKEND_BEDROCK:
        pass  # Uses IAM roles, hard to pre-check

    return {
        "ready": len(issues) == 0,
        "backend": backend,
        "issues": issues,
    }
