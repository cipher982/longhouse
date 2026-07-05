"""Private implementation helpers for the session_chat router.

Contains all managed-local launch, stream, lock, and dispatch logic that
is too large to live inline in the router endpoints.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import AsyncIterator
from uuid import UUID

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import zerg.services.live_session_dispatch as live_session_dispatch
from zerg.metrics import managed_turn_dispatch_seconds
from zerg.metrics import managed_turn_phase_seconds
from zerg.metrics import managed_turn_requests_total
from zerg.metrics import managed_turn_wait_seconds
from zerg.metrics import managed_turn_wait_total
from zerg.models.agents import AgentEvent
from zerg.models.agents import SessionRuntimeState
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.models_config import get_llm_client_for_use_case
from zerg.observability import get_tracer
from zerg.observability import mark_span_error
from zerg.observability import set_span_attributes
from zerg.services.agents import AgentsStore
from zerg.services.managed_local_control import MANAGED_LOCAL_CONTROL_STATUS_COMPLETED
from zerg.services.managed_local_control import MANAGED_LOCAL_CONTROL_STATUS_FAILED
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_COMPLETE
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_FAILED
from zerg.services.managed_local_control import MANAGED_LOCAL_SYNC_STATUS_PENDING
from zerg.services.managed_local_control import ManagedLocalTerminalResult
from zerg.services.managed_local_control import await_managed_local_hook_phase_update
from zerg.services.managed_local_control import await_managed_local_turn_terminal
from zerg.services.managed_local_control import get_managed_local_control_status_for_phase
from zerg.services.managed_local_control import get_managed_local_latest_hook_observation_id
from zerg.services.managed_local_event_polling import MANAGED_LOCAL_EVENT_TIMEOUT_SECS
from zerg.services.managed_local_event_polling import MANAGED_LOCAL_POLL_INTERVAL_SECS
from zerg.services.managed_local_event_polling import await_managed_local_events_task
from zerg.services.managed_local_event_polling import await_managed_local_terminal_task
from zerg.services.managed_local_event_polling import await_managed_local_turn_events
from zerg.services.managed_local_event_polling import fetch_managed_local_events_between_ids
from zerg.services.managed_local_event_polling import fetch_managed_local_events_since
from zerg.services.managed_local_event_polling import get_managed_local_latest_event_id
from zerg.services.managed_local_event_polling import get_session_turn_snapshot_best_effort
from zerg.services.managed_local_event_polling import hydrate_turn_events_from_snapshot
from zerg.services.session_current_control import current_session_capabilities
from zerg.services.session_kernel_projection import project_session_kernel_fields
from zerg.services.session_kernel_projection import session_lock_scope_id
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_runtime import session_is_closed_for_input
from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED
from zerg.services.session_turns import SESSION_TURN_ERROR_TURN_TIMEOUT
from zerg.services.session_turns import SESSION_TURN_ERROR_VERIFICATION_TIMEOUT
from zerg.services.session_turns import create_session_turn
from zerg.services.session_turns import execute_best_effort_session_turn_write
from zerg.services.session_turns import execute_session_turn_write
from zerg.services.session_turns import mark_session_turn_active
from zerg.services.session_turns import mark_session_turn_failed
from zerg.services.session_turns import mark_session_turn_send_accepted
from zerg.services.session_turns import mark_session_turn_terminal
from zerg.services.session_turns import maybe_mark_session_turn_durable
from zerg.services.session_turns import run_best_effort_session_turn_write
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_loop_mode import SessionLoopMode
from zerg.session_loop_mode import coerce_session_loop_mode
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

_CURRENT_SESSION_HEADER = "X-Longhouse-Session-Id"
MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS = 300.0
MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS = 10.0
MANAGED_LOCAL_POST_DURABLE_TERMINAL_GRACE_SECS = 0.5
MANAGED_LOCAL_TURN_ARCHIVE_ACK_TIMEOUT_SECS = 0.25
_MANAGED_LOCAL_ACTIVE_PHASES = frozenset({"thinking", "running"})
_MANAGED_LOCAL_TERMINAL_PHASES = frozenset({"idle", "needs_user", "blocked"})
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
_DRAFT_REPLY_EVENT_LIMIT = 80
_DRAFT_REPLY_EVENT_CHAR_LIMIT = 1800


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


# ---------------------------------------------------------------------------
# Response models (shared between impl and router)
# ---------------------------------------------------------------------------


class SessionLockInfo(BaseModel):
    """Information about a session lock."""

    locked: bool
    holder: str | None = None
    time_remaining_seconds: float | None = None
    fork_available: bool = False


class ManagedLocalSessionLaunchResponse(BaseModel):
    """Response after successfully starting a managed local session."""

    session_id: str
    provider: str
    provider_session_id: str | None = None
    execution_home: SessionExecutionHome
    managed_transport: ManagedSessionTransport
    loop_mode: SessionLoopMode
    source_runner_id: int | None = None
    source_runner_name: str
    managed_session_name: str
    attach_command: str
    permission_mode: str = "bypass"
    # Session-scoped hook token, only present for remote_approve launches. The CLI
    # exports it as LONGHOUSE_HOOK_TOKEN so the permission gate authenticates as
    # this session (the permission-gate endpoints reject durable device tokens).
    hook_token: str | None = None


class SessionDraftReplyResponse(BaseModel):
    """Suggested next user message for a managed session."""

    draft_text: str
    model: str
    generated_at: datetime
    based_on_event_ids: list[int]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


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


def _validate_managed_local_launch_response_contract(
    *,
    session_id: str,
    response: "ManagedLocalSessionLaunchResponse",
) -> None:
    sid = str(session_id or "").strip()
    provider = str(response.provider or "").strip()
    source_runner_name = str(response.source_runner_name or "").strip()
    attach_command = str(response.attach_command or "")
    transport = response.managed_transport
    if not sid or str(response.session_id) != sid:
        raise RuntimeError("Managed local launch response has mismatched session id")
    if not provider:
        raise RuntimeError("Managed local launch response is missing provider")
    if transport is None:
        raise RuntimeError("Managed local launch response is missing managed transport")
    if not source_runner_name:
        raise RuntimeError("Managed local launch response is missing source runner name")

    if transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE:
        provider_sid = str(response.provider_session_id or "").strip()
        if not provider_sid:
            raise RuntimeError("Claude managed local launch response is missing provider_session_id")
        if provider_sid == sid:
            raise RuntimeError("Claude managed local launch response has synthetic provider_session_id")
        if provider_sid not in attach_command:
            raise RuntimeError("Claude managed local launch response attach_command does not target provider_session_id")
        if "LONGHOUSE_PROVIDER_SESSION_ID" not in attach_command:
            raise RuntimeError("Claude managed local launch response attach_command is missing provider id env")
        return

    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        if "codex-bridge attach --session-id" not in attach_command or sid not in attach_command:
            raise RuntimeError("Codex managed local launch response is missing codex bridge attach command")
        return

    if transport == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE:
        if "opencode-channel attach --session-id" not in attach_command or sid not in attach_command:
            raise RuntimeError("OpenCode managed local launch response is missing opencode channel attach command")
        return

    if transport == ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX:
        if attach_command:
            raise RuntimeError("Antigravity managed local launch response should not include an attach command")
        return

    if transport == ManagedSessionTransport.CURSOR_HELM:
        # Helm is a PTY pass-through running in the user's terminal; there is no
        # separate attach command (you are already attached via the TUI), so the
        # response should carry an empty attach_command like Antigravity.
        if attach_command:
            raise RuntimeError("Cursor Helm managed local launch response should not include an attach command")
        return

    raise RuntimeError(f"Unsupported managed local launch response transport: {transport}")


def _managed_local_launch_response(db: Session, result, *, owner_id: int | None = None) -> ManagedLocalSessionLaunchResponse:
    session = result.session
    kernel_projection = project_session_kernel_fields(db, session)
    capabilities = kernel_projection.capabilities
    # The kernel projection is the truth: a launch response is only valid if
    # the kernel rows actually grant managed control. ``execution_home`` /
    # ``managed_transport`` columns are no longer authoritative.
    if not (capabilities.live_control_available or capabilities.host_reattach_available):
        raise RuntimeError("Managed local launch response requires a kernel-managed session")
    if capabilities.managed_transport is None:
        raise RuntimeError("Managed local launch response is missing managed transport metadata")
    control_projection = kernel_projection.control
    permission_mode = str(getattr(session, "permission_mode", "") or "bypass").strip() or "bypass"
    # Mint a session-scoped hook token ONLY for remote_approve launches, so the
    # permission gate can authenticate as this exact session (the gate endpoints
    # reject durable device tokens). Bypass launches never get one.
    hook_token: str | None = None
    if permission_mode == "remote_approve" and owner_id is not None:
        from zerg.auth.managed_local_hook_tokens import issue_managed_local_hook_token

        hook_token = issue_managed_local_hook_token(
            owner_id=owner_id,
            session_id=str(session.id),
            project=getattr(session, "project", None),
            device_id=getattr(session, "device_id", None),
        )
    response = ManagedLocalSessionLaunchResponse(
        session_id=str(session.id),
        provider=session.provider or "claude",
        provider_session_id=kernel_projection.provider_session_id,
        execution_home=capabilities.execution_home,
        managed_transport=capabilities.managed_transport,
        loop_mode=coerce_session_loop_mode(session.loop_mode),
        source_runner_id=control_projection.source_runner_id,
        source_runner_name=control_projection.source_runner_name or "",
        managed_session_name=control_projection.managed_session_name or "",
        attach_command=result.attach_command,
        permission_mode=permission_mode,
        hook_token=hook_token,
    )
    _validate_managed_local_launch_response_contract(
        session_id=str(session.id),
        response=response,
    )
    return response


def _event_content_for_draft(event: AgentEvent) -> str:
    if event.tool_name:
        payload = event.content_text or event.tool_output_text or ""
        if event.tool_input_json:
            try:
                payload = f"input={json.dumps(event.tool_input_json, sort_keys=True)}\n{payload}".strip()
            except TypeError:
                payload = str(event.tool_input_json)
        return f"{event.role} tool {event.tool_name}: {payload}".strip()
    if event.role == "tool":
        return f"tool result: {event.tool_output_text or event.content_text or ''}".strip()
    return f"{event.role}: {event.content_text or ''}".strip()


def _format_event_for_draft(event: AgentEvent) -> str:
    text = _event_content_for_draft(event).strip()
    if len(text) > _DRAFT_REPLY_EVENT_CHAR_LIMIT:
        text = f"{text[:_DRAFT_REPLY_EVENT_CHAR_LIMIT].rstrip()} ..."
    return f"[{event.id}] {text}"


def _build_draft_reply_messages(*, source_session, events: list[AgentEvent], max_chars: int) -> list[dict[str, str]]:
    transcript = "\n\n".join(_format_event_for_draft(event) for event in events)
    metadata_lines = [
        f"provider: {source_session.provider or 'unknown'}",
        f"project: {source_session.project or 'unknown'}",
        f"cwd: {source_session.cwd or 'unknown'}",
        f"git_branch: {source_session.git_branch or 'unknown'}",
        f"session_status: {getattr(source_session, 'status', None) or 'unknown'}",
    ]
    system = (
        "You draft the next human operator message for a coding-agent session. "
        "Return only the message text. Do not send the message. Do not include explanations, "
        "markdown fences, labels, or alternatives. Keep it concise, actionable, and faithful to "
        "the transcript. If the right next step is unclear, ask the agent for the smallest useful "
        "clarification or status update. Never claim that the user approved, tested, or performed "
        "work unless that is explicit in the transcript."
    )
    user = f"Draft one next user message of at most {max_chars} characters.\n\n" "Session metadata:\n" + "\n".join(
        metadata_lines
    ) + "\n\nRecent transcript tail:\n" + (transcript or "(no transcript events)")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def _close_llm_client(client) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _build_managed_local_draft_reply_response(
    *,
    source_session,
    request_id: str,
    max_chars: int,
    db: Session,
    owner_id: int | None = None,
) -> SessionDraftReplyResponse:
    _assert_live_session_send_available(db, source_session, owner_id=owner_id)

    events = AgentsStore(db).get_session_events(
        source_session.id,
        branch_mode="head",
        limit=_DRAFT_REPLY_EVENT_LIMIT,
        load_from_end=True,
    )
    if not events:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This session has no transcript events to draft from.",
        )

    try:
        client, model, _provider = get_llm_client_for_use_case("summarization")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Draft reply is unavailable because no text LLM provider is configured.",
        ) from exc

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=_build_draft_reply_messages(source_session=source_session, events=events, max_chars=max_chars),
        )
    except Exception as exc:
        logger.exception("[%s] Draft reply generation failed for session %s", request_id, source_session.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Draft reply generation failed.",
        ) from exc
    finally:
        await _close_llm_client(client)

    raw = response.choices[0].message.content if getattr(response, "choices", None) else ""
    draft_text = str(raw or "").strip()
    if not draft_text:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Draft reply generation returned an empty response.",
        )
    if len(draft_text) > max_chars:
        draft_text = draft_text[:max_chars].rstrip()

    return SessionDraftReplyResponse(
        draft_text=draft_text,
        model=str(model),
        generated_at=datetime.now(timezone.utc),
        based_on_event_ids=[event.id for event in events],
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


def _assert_live_session_send_available(
    db: Session,
    source_session,
    *,
    owner_id: int | None = None,
) -> None:
    if _session_is_closed_for_input(db, source_session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "session_closed",
                "message": "This session has ended.",
            },
        )
    capabilities = current_session_capabilities(db, source_session, owner_id=owner_id)
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


def _session_is_closed_for_input(db: Session, source_session) -> bool:
    session_id = getattr(source_session, "id", None)
    return session_is_closed_for_input(db, session_id)


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


async def _build_managed_local_chat_response(
    *,
    source_session,
    owner_id: int,
    message: str,
    request_id: str,
    lock_scope_id: str,
    db: Session,
    session_input_id: int | None = None,
    attachments: list[dict] | None = None,
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
        session_input_id=session_input_id,
        attachments=attachments,
    )


async def _release_managed_local_lock_after_terminal(
    *,
    lock_scope_id: str,
    request_id: str,
    session_id: UUID,
    provider: str,
    db_bind,
    after_observation_id: int,
) -> None:
    tracer = get_tracer(__name__)
    wait_started = time.monotonic()
    with tracer.start_as_current_span("longhouse.turn.wait_terminal") as span:
        wait_started_at = datetime.now(timezone.utc)
        set_span_attributes(
            span,
            {
                "longhouse.provider": provider,
                "longhouse.managed": True,
                "longhouse.session.id": session_id,
                "longhouse.turn.request_id": request_id,
                "longhouse.turn.after_observation_id": after_observation_id,
                "longhouse.turn.timeout_secs": MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS,
            },
        )
        try:
            terminal_result = await await_managed_local_turn_terminal(
                db_bind=db_bind,
                session_id=session_id,
                after_observation_id=after_observation_id,
                timeout_secs=MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS,
            )
        except Exception as exc:
            wait_seconds = max(0.0, time.monotonic() - wait_started)
            managed_turn_wait_total.labels(provider=provider, milestone="terminal", outcome="error").inc()
            managed_turn_wait_seconds.labels(provider=provider, milestone="terminal", outcome="error").observe(wait_seconds)
            mark_span_error(span, exc)
            logger.warning(
                "[%s] Managed-local lock watcher crashed for %s",
                request_id,
                session_id,
                exc_info=True,
            )
            return

        if terminal_result is None:
            terminal_result = _runtime_terminal_result_after(
                db_bind=db_bind,
                session_id=session_id,
                after=wait_started_at,
            )
            if terminal_result is not None:
                logger.info(
                    "[%s] Managed-local lock watcher recovered terminal phase %s for %s from runtime state",
                    request_id,
                    terminal_result.phase,
                    session_id,
                )

        if terminal_result is None:
            wait_seconds = max(0.0, time.monotonic() - wait_started)
            managed_turn_wait_total.labels(provider=provider, milestone="terminal", outcome="timeout").inc()
            managed_turn_wait_seconds.labels(provider=provider, milestone="terminal", outcome="timeout").observe(wait_seconds)
            set_span_attributes(span, {"longhouse.turn.outcome": "timeout"})
            logger.warning(
                "[%s] Managed-local lock watcher timed out for %s; leaving TTL lock in place",
                request_id,
                session_id,
            )
            return

        set_span_attributes(
            span,
            {
                "longhouse.turn.outcome": "terminal_observed",
                "longhouse.turn.terminal_phase": terminal_result.phase,
                "longhouse.turn.terminal_at": terminal_result.occurred_at,
            },
        )
        wait_seconds = max(0.0, time.monotonic() - wait_started)
        managed_turn_wait_total.labels(provider=provider, milestone="terminal", outcome="observed").inc()
        managed_turn_wait_seconds.labels(provider=provider, milestone="terminal", outcome="observed").observe(wait_seconds)

        try:
            with tracer.start_as_current_span("longhouse.turn.persist_terminal") as persist_span:
                updated_session_turn = await execute_session_turn_write(
                    db_bind=db_bind,
                    label="session-turn-terminal",
                    fn=lambda turn_db: mark_session_turn_terminal(
                        turn_db,
                        session_id=session_id,
                        request_id=request_id,
                        phase=terminal_result.phase,
                        terminal_at=terminal_result.occurred_at,
                    ),
                )
                set_span_attributes(
                    persist_span,
                    {
                        "longhouse.session.id": session_id,
                        "longhouse.turn.request_id": request_id,
                        "longhouse.turn.updated": bool(updated_session_turn),
                    },
                )
                if not updated_session_turn:
                    logger.warning(
                        "[%s] Managed-local terminal watcher saw %s for %s but canonical turn update did not apply",
                        request_id,
                        terminal_result.phase,
                        session_id,
                    )
        except Exception as exc:
            mark_span_error(span, exc)
            logger.warning(
                "[%s] Managed-local terminal watcher failed to persist terminal state for %s",
                request_id,
                session_id,
                exc_info=True,
            )

        try:
            from zerg.services.session_inputs import mark_delivery_attempt_completed

            await execute_session_turn_write(
                db_bind=db_bind,
                label="session-input-attempt-completed",
                fn=lambda attempt_db: mark_delivery_attempt_completed(
                    attempt_db,
                    session_id=session_id,
                    request_id=request_id,
                    completed_at=terminal_result.occurred_at,
                ),
            )
        except Exception:
            logger.warning(
                "[%s] Managed-local terminal watcher failed to mark delivery attempt completed for %s",
                request_id,
                session_id,
                exc_info=True,
            )

        with tracer.start_as_current_span("longhouse.turn.lock_release") as release_span:
            released = await session_lock_manager.release(lock_scope_id, request_id)
            set_span_attributes(
                release_span,
                {
                    "longhouse.session.id": session_id,
                    "longhouse.turn.request_id": request_id,
                    "longhouse.turn.lock_released": released,
                },
            )
        logger.info(
            "[%s] Managed-local session reached terminal phase %s; lock release=%s",
            request_id,
            terminal_result.phase,
            released,
        )

        # Drain the oldest queued SessionInput, if any. Runs in a fresh DB
        # session bound to the same engine; reacquires the session lock via
        # the normal send path so a racing user send can't double-dispatch.
        try:
            from zerg.services.session_input_queue import wake_session_input_queue

            await wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="turn_terminal",
                lock_scope_id=lock_scope_id,
            )
        except Exception:
            logger.exception(
                "[%s] Drain of queued SessionInput failed for %s (non-fatal)",
                request_id,
                session_id,
            )


def _runtime_terminal_result_after(*, db_bind, session_id: UUID, after: datetime) -> ManagedLocalTerminalResult | None:
    """Recover lock release from fresh runtime state when observation polling misses idle.

    The bridge/runtime reducer is an independent live truth lane. If it has
    observed a terminal-ish phase after the send watcher started, do not leave
    queued inputs waiting for an unrelated future runtime event.
    """
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db_bind, expire_on_commit=False)
    db = Session()
    try:
        state = (
            db.query(SessionRuntimeState)
            .filter(SessionRuntimeState.session_id == session_id)
            .order_by(SessionRuntimeState.updated_at.desc())
            .first()
        )
        if state is None:
            return None
        phase = str(getattr(state, "phase", "") or "").strip()
        if not phase or phase not in _MANAGED_LOCAL_TERMINAL_PHASES:
            return None
        observed_at = (
            normalize_utc(getattr(state, "phase_started_at", None))
            or normalize_utc(getattr(state, "last_runtime_signal_at", None))
            or normalize_utc(getattr(state, "updated_at", None))
        )
        after_utc = normalize_utc(after) or after
        if observed_at is None or observed_at < after_utc:
            return None
        return ManagedLocalTerminalResult(
            phase=phase,
            control_status=get_managed_local_control_status_for_phase(phase),
            observation_id=0,
            occurred_at=observed_at,
        )
    finally:
        db.close()


async def _drain_next_queued_input(
    *,
    db_bind,
    session_id: UUID,
    lock_scope_id: str | None = None,
) -> None:
    """Compatibility shim for the managed input queue wake service."""
    from zerg.services.session_input_queue import wake_session_input_queue

    await wake_session_input_queue(
        db_bind=db_bind,
        session_id=session_id,
        reason="legacy_drain",
        lock_scope_id=lock_scope_id,
    )


async def _observe_managed_local_turn_active_phase(
    *,
    request_id: str,
    session_id: UUID,
    provider: str,
    db_bind,
    after_observation_id: int,
) -> None:
    tracer = get_tracer(__name__)
    wait_started = time.monotonic()
    with tracer.start_as_current_span("longhouse.turn.wait_active") as span:
        set_span_attributes(
            span,
            {
                "longhouse.provider": provider,
                "longhouse.managed": True,
                "longhouse.session.id": session_id,
                "longhouse.turn.request_id": request_id,
                "longhouse.turn.after_observation_id": after_observation_id,
                "longhouse.turn.active_phases": tuple(sorted(_MANAGED_LOCAL_ACTIVE_PHASES)),
                "longhouse.turn.timeout_secs": MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS,
            },
        )
        try:
            active_update = await await_managed_local_hook_phase_update(
                db_bind=db_bind,
                session_id=session_id,
                after_observation_id=after_observation_id,
                phases=set(_MANAGED_LOCAL_ACTIVE_PHASES),
                timeout_secs=MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS,
                poll_interval_secs=MANAGED_LOCAL_POLL_INTERVAL_SECS,
            )
        except Exception as exc:
            wait_seconds = max(0.0, time.monotonic() - wait_started)
            managed_turn_wait_total.labels(provider=provider, milestone="active", outcome="error").inc()
            managed_turn_wait_seconds.labels(provider=provider, milestone="active", outcome="error").observe(wait_seconds)
            mark_span_error(span, exc)
            logger.warning(
                "[%s] Managed-local active watcher crashed for %s",
                request_id,
                session_id,
                exc_info=True,
            )
            return

        if active_update is None:
            wait_seconds = max(0.0, time.monotonic() - wait_started)
            managed_turn_wait_total.labels(provider=provider, milestone="active", outcome="timeout").inc()
            managed_turn_wait_seconds.labels(provider=provider, milestone="active", outcome="timeout").observe(wait_seconds)
            set_span_attributes(span, {"longhouse.turn.outcome": "timeout"})
            return

        set_span_attributes(
            span,
            {
                "longhouse.turn.outcome": "active_observed",
                "longhouse.turn.active_phase": active_update.phase,
                "longhouse.turn.active_phase_observed_at": active_update.occurred_at,
            },
        )
        wait_seconds = max(0.0, time.monotonic() - wait_started)
        managed_turn_wait_total.labels(provider=provider, milestone="active", outcome="observed").inc()
        managed_turn_wait_seconds.labels(provider=provider, milestone="active", outcome="observed").observe(wait_seconds)
        try:
            with tracer.start_as_current_span("longhouse.turn.persist_active") as persist_span:
                updated = await execute_session_turn_write(
                    db_bind=db_bind,
                    label="session-turn-active",
                    fn=lambda turn_db: mark_session_turn_active(
                        turn_db,
                        session_id=session_id,
                        request_id=request_id,
                        observed_at=active_update.occurred_at,
                    ),
                )
                set_span_attributes(
                    persist_span,
                    {
                        "longhouse.session.id": session_id,
                        "longhouse.turn.request_id": request_id,
                        "longhouse.turn.updated": bool(updated),
                    },
                )
                if not updated:
                    logger.debug(
                        "[%s] Managed-local active watcher saw %s for %s but no canonical update was needed",
                        request_id,
                        active_update.phase,
                        session_id,
                    )
        except Exception as exc:
            mark_span_error(span, exc)
            logger.warning(
                "[%s] Managed-local active watcher failed to persist active phase for %s",
                request_id,
                session_id,
                exc_info=True,
            )


def _schedule_managed_local_active_phase_observation(
    *,
    request_id: str,
    session_id: UUID,
    provider: str,
    db_bind,
    after_observation_id: int,
) -> None:
    task = asyncio.create_task(
        _observe_managed_local_turn_active_phase(
            request_id=request_id,
            session_id=session_id,
            provider=provider,
            db_bind=db_bind,
            after_observation_id=after_observation_id,
        )
    )

    def _log_task_failure(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            logger.debug("[%s] Managed-local active watcher cancelled for %s", request_id, session_id)
        except Exception:
            logger.exception("[%s] Managed-local active watcher failed for %s", request_id, session_id)

    task.add_done_callback(_log_task_failure)


def _schedule_managed_local_lock_release(
    *,
    lock_scope_id: str,
    request_id: str,
    session_id: UUID,
    provider: str,
    db_bind,
    after_observation_id: int,
) -> None:
    task = asyncio.create_task(
        _release_managed_local_lock_after_terminal(
            lock_scope_id=lock_scope_id,
            request_id=request_id,
            session_id=session_id,
            provider=provider,
            db_bind=db_bind,
            after_observation_id=after_observation_id,
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


def _managed_local_send_failure_code(send_result) -> str:
    if bool(getattr(send_result, "ok", False)) or int(getattr(send_result, "exit_code", 1) or 1) == 0:
        return SESSION_TURN_ERROR_VERIFICATION_TIMEOUT
    return SESSION_TURN_ERROR_SEND_FAILED


def _mark_managed_local_turn_send_accepted(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    baseline_event_id: int,
    baseline_observation_cursor: int,
    user_submitted_at: datetime,
    expected_user_text: str,
    accepted_at: datetime,
    user_event_id: int | None,
    session_input_id: int | None,
) -> bool:
    create_session_turn(
        db,
        session_id=session_id,
        request_id=request_id,
        baseline_event_id=baseline_event_id,
        baseline_observation_cursor=baseline_observation_cursor,
        user_submitted_at=user_submitted_at,
        expected_user_text=expected_user_text,
        session_input_id=session_input_id,
    )
    return mark_session_turn_send_accepted(
        db,
        session_id=session_id,
        request_id=request_id,
        accepted_at=accepted_at,
        user_event_id=user_event_id,
        session_input_id=session_input_id,
    )


def _mark_managed_local_turn_failed_for_send(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    baseline_event_id: int,
    baseline_observation_cursor: int,
    user_submitted_at: datetime,
    expected_user_text: str,
    accepted_at: datetime,
    user_event_id: int | None,
    session_input_id: int | None,
    error_code: str,
) -> bool:
    create_session_turn(
        db,
        session_id=session_id,
        request_id=request_id,
        baseline_event_id=baseline_event_id,
        baseline_observation_cursor=baseline_observation_cursor,
        user_submitted_at=user_submitted_at,
        expected_user_text=expected_user_text,
        session_input_id=session_input_id,
    )
    if error_code == SESSION_TURN_ERROR_VERIFICATION_TIMEOUT:
        mark_session_turn_send_accepted(
            db,
            session_id=session_id,
            request_id=request_id,
            accepted_at=accepted_at,
            user_event_id=user_event_id,
            session_input_id=session_input_id,
        )
    return mark_session_turn_failed(
        db,
        session_id=session_id,
        request_id=request_id,
        error_code=error_code,
    )


async def _dispatch_managed_local_text(
    *,
    source_session,
    owner_id: int,
    message: str,
    request_id: str,
    lock_scope_id: str,
    db: Session,
    session_input_id: int | None = None,
    attachments: list[dict] | None = None,
) -> JSONResponse:
    """Send text to a managed-local session and return acceptance status."""
    tracer = get_tracer(__name__)
    provider_label = source_session.provider or "claude"
    with tracer.start_as_current_span("longhouse.turn") as span:
        t0 = time.monotonic()
        set_span_attributes(
            span,
            {
                "longhouse.provider": provider_label,
                "longhouse.managed": True,
                "longhouse.turn.control_path": "managed_local",
                "longhouse.session.id": source_session.id,
                "longhouse.turn.request_id": request_id,
                "longhouse.turn.lock_scope_id": lock_scope_id,
            },
        )

        if not current_session_capabilities(db, source_session, owner_id=owner_id).live_control_available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Managed local session is missing live runner metadata",
            )

        with tracer.start_as_current_span("longhouse.turn.baseline") as baseline_span:
            baseline_event_id = int(AgentsStore(db).get_latest_event_id(source_session.id) or 0)
            baseline_hook_observation_id = get_managed_local_latest_hook_observation_id(
                db=db,
                session_id=source_session.id,
            )
            user_submitted_at = datetime.now(timezone.utc)
            set_span_attributes(
                baseline_span,
                {
                    "longhouse.session.id": source_session.id,
                    "longhouse.turn.request_id": request_id,
                    "longhouse.turn.baseline_event_id": baseline_event_id,
                    "longhouse.turn.baseline_observation_id": baseline_hook_observation_id,
                    "longhouse.turn.user_submitted_at": user_submitted_at,
                },
            )
        t_baseline = time.monotonic()

        with tracer.start_as_current_span("longhouse.turn.persist_create") as create_span:
            await execute_best_effort_session_turn_write(
                db_bind=db.get_bind(),
                label="turn-create",
                timeout_seconds=MANAGED_LOCAL_TURN_ARCHIVE_ACK_TIMEOUT_SECS,
                fn=lambda turn_db: create_session_turn(
                    turn_db,
                    session_id=source_session.id,
                    request_id=request_id,
                    baseline_event_id=baseline_event_id,
                    baseline_observation_cursor=baseline_hook_observation_id,
                    user_submitted_at=user_submitted_at,
                    expected_user_text=message,
                    session_input_id=session_input_id,
                ),
            )
            set_span_attributes(
                create_span,
                {
                    "longhouse.session.id": source_session.id,
                    "longhouse.turn.request_id": request_id,
                },
            )
        t_turn_created = time.monotonic()

        with tracer.start_as_current_span("longhouse.turn.provider_dispatch") as dispatch_span:
            send_result = await live_session_dispatch.send_text_to_live_session(
                db=db,
                owner_id=owner_id,
                session=source_session,
                text=message,
                request_id=request_id,
                timeout_secs=15,
                verify_turn_started=True,
                verification_timeout_secs=15.0,
                attachments=attachments,
            )
            send_observed_at = datetime.now(timezone.utc)
            set_span_attributes(
                dispatch_span,
                {
                    "longhouse.session.id": source_session.id,
                    "longhouse.turn.request_id": request_id,
                    "longhouse.turn.send_observed_at": send_observed_at,
                    "longhouse.turn.dispatch_ok": bool(send_result.ok),
                    "longhouse.turn.turn_verified": bool(getattr(send_result, "verified_turn_started", False)),
                    "longhouse.turn.user_event_id": getattr(send_result, "verified_user_event_id", None),
                    "longhouse.turn.exit_code": getattr(send_result, "exit_code", None),
                },
            )
        t_sent = time.monotonic()

        if not send_result.ok or not bool(getattr(send_result, "verified_turn_started", False)):
            error_code = _managed_local_send_failure_code(send_result)
            with tracer.start_as_current_span("longhouse.turn.persist_send_result") as persist_span:
                await execute_best_effort_session_turn_write(
                    db_bind=db.get_bind(),
                    label="turn-send-failed",
                    timeout_seconds=MANAGED_LOCAL_TURN_ARCHIVE_ACK_TIMEOUT_SECS,
                    fn=lambda turn_db: _mark_managed_local_turn_failed_for_send(
                        turn_db,
                        session_id=source_session.id,
                        request_id=request_id,
                        baseline_event_id=baseline_event_id,
                        baseline_observation_cursor=baseline_hook_observation_id,
                        user_submitted_at=user_submitted_at,
                        expected_user_text=message,
                        accepted_at=send_observed_at,
                        user_event_id=getattr(send_result, "verified_user_event_id", None),
                        session_input_id=session_input_id,
                        error_code=error_code,
                    ),
                )
                set_span_attributes(
                    persist_span,
                    {
                        "longhouse.session.id": source_session.id,
                        "longhouse.turn.request_id": request_id,
                        "longhouse.turn.error_code": error_code,
                    },
                )
            error_message = str(send_result.error or "Managed local session did not acknowledge the prompt after send")
            mark_span_error(span, error_message)
            set_span_attributes(
                span,
                {
                    "longhouse.turn.outcome": "failed",
                    "longhouse.turn.error_code": error_code,
                },
            )
            dispatch_seconds = max(0.0, time.monotonic() - t0)
            managed_turn_requests_total.labels(provider=provider_label, outcome="failed").inc()
            managed_turn_dispatch_seconds.labels(provider=provider_label).observe(dispatch_seconds)
            logger.warning(
                "[%s] Managed-local send-live failed for %s: error_code=%s verified=%s exit_code=%s error=%s",
                request_id,
                source_session.id,
                error_code,
                bool(getattr(send_result, "verified_turn_started", False)),
                getattr(send_result, "exit_code", None),
                error_message,
            )
            with tracer.start_as_current_span("longhouse.turn.lock_release") as release_span:
                released = await session_lock_manager.release(lock_scope_id, request_id)
                set_span_attributes(
                    release_span,
                    {
                        "longhouse.session.id": source_session.id,
                        "longhouse.turn.request_id": request_id,
                        "longhouse.turn.lock_released": released,
                    },
                )
            logger.info(f"[{request_id}] Managed local chat dispatch failed, lock released")
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "accepted": False,
                    "error": error_message,
                    "error_code": error_code,
                    "session_id": str(source_session.id),
                    "request_id": request_id,
                },
            )

        with tracer.start_as_current_span("longhouse.turn.persist_send_result") as persist_span:
            await execute_best_effort_session_turn_write(
                db_bind=db.get_bind(),
                label="turn-send-accepted",
                timeout_seconds=MANAGED_LOCAL_TURN_ARCHIVE_ACK_TIMEOUT_SECS,
                fn=lambda turn_db: _mark_managed_local_turn_send_accepted(
                    turn_db,
                    session_id=source_session.id,
                    request_id=request_id,
                    baseline_event_id=baseline_event_id,
                    baseline_observation_cursor=baseline_hook_observation_id,
                    user_submitted_at=user_submitted_at,
                    expected_user_text=message,
                    accepted_at=send_observed_at,
                    user_event_id=getattr(send_result, "verified_user_event_id", None),
                    session_input_id=session_input_id,
                ),
            )
            set_span_attributes(
                persist_span,
                {
                    "longhouse.session.id": source_session.id,
                    "longhouse.turn.request_id": request_id,
                    "longhouse.turn.user_event_id": getattr(send_result, "verified_user_event_id", None),
                },
            )

        _schedule_managed_local_active_phase_observation(
            request_id=request_id,
            session_id=source_session.id,
            provider=provider_label,
            db_bind=db.get_bind(),
            after_observation_id=baseline_hook_observation_id,
        )
        _schedule_managed_local_lock_release(
            lock_scope_id=lock_scope_id,
            request_id=request_id,
            session_id=source_session.id,
            provider=provider_label,
            db_bind=db.get_bind(),
            after_observation_id=baseline_hook_observation_id,
        )

        baseline_ms = round((t_baseline - t0) * 1000, 1)
        turn_create_ms = round((t_turn_created - t_baseline) * 1000, 1)
        provider_dispatch_ms = round((t_sent - t_turn_created) * 1000, 1)
        dispatch_ms = round((t_sent - t0) * 1000, 1)
        managed_turn_requests_total.labels(provider=provider_label, outcome="send_accepted").inc()
        managed_turn_dispatch_seconds.labels(provider=provider_label).observe(dispatch_ms / 1000.0)
        managed_turn_phase_seconds.labels(provider=provider_label, phase="baseline").observe(baseline_ms / 1000.0)
        managed_turn_phase_seconds.labels(provider=provider_label, phase="turn_create").observe(turn_create_ms / 1000.0)
        managed_turn_phase_seconds.labels(provider=provider_label, phase="provider_dispatch").observe(provider_dispatch_ms / 1000.0)
        set_span_attributes(
            span,
            {
                "longhouse.turn.outcome": "send_accepted",
                "longhouse.turn.baseline_event_id": baseline_event_id,
                "longhouse.turn.baseline_observation_id": baseline_hook_observation_id,
                "longhouse.turn.phase_ms.baseline": baseline_ms,
                "longhouse.turn.phase_ms.turn_create": turn_create_ms,
                "longhouse.turn.phase_ms.provider_dispatch": provider_dispatch_ms,
                "longhouse.turn.phase_ms.total": dispatch_ms,
            },
        )
        logger.info(
            "[%s] managed-local dispatch: baseline=%.0fms turn_create=%.0fms send=%.0fms total=%.0fms",
            request_id,
            baseline_ms,
            turn_create_ms,
            provider_dispatch_ms,
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


def _lock_scope_id_for_session(db: Session, session_id: str) -> str:
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return session_id
    session = AgentsStore(db).get_session(session_uuid)
    if session is None:
        return session_id
    return session_lock_scope_id(session.id)


async def _acquire_session_lock_or_raise(*, source_session, request_id: str) -> str:
    lock_scope_id = session_lock_scope_id(source_session.id)
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


_fetch_managed_local_events_since = fetch_managed_local_events_since
_fetch_managed_local_events_between_ids = fetch_managed_local_events_between_ids
_get_managed_local_latest_event_id = get_managed_local_latest_event_id
_await_managed_local_turn_events = await_managed_local_turn_events
_await_managed_local_events_task = await_managed_local_events_task
_await_managed_local_terminal_task = await_managed_local_terminal_task


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
    capabilities = current_session_capabilities(db, source_session, owner_id=owner_id)
    if not capabilities.live_control_available:
        raise RuntimeError("Managed local session is missing live runner metadata")
    kernel_projection = project_session_kernel_fields(db, source_session, capabilities=capabilities)
    lineage_projection = kernel_projection.lineage
    control_projection = kernel_projection.control

    yield SSEEvent(
        event="system",
        data=json.dumps(
            {
                "type": "session_started",
                "session_id": str(source_session.id),
                "source_session_id": str(source_session.id),
                "thread_root_session_id": lineage_projection.thread_root_session_id,
                "continued_from_session_id": lineage_projection.continued_from_session_id,
                "created_branch": False,
                "provider_session_id": kernel_projection.provider_session_id,
                "execution_home": capabilities.execution_home.value,
                "origin_label": lineage_projection.origin_label,
                "runner_name": control_projection.source_runner_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ),
    ).encode()

    baseline_event_id = int(AgentsStore(db).get_latest_event_id(source_session.id) or 0)
    baseline_hook_observation_id = get_managed_local_latest_hook_observation_id(
        db=db,
        session_id=source_session.id,
    )
    run_best_effort_session_turn_write(
        db_bind=db.get_bind(),
        label="create",
        fn=lambda turn_db: create_session_turn(
            turn_db,
            session_id=source_session.id,
            request_id=request_id,
            baseline_event_id=baseline_event_id,
            baseline_observation_cursor=baseline_hook_observation_id,
            expected_user_text=message,
        ),
    )
    send_result = await live_session_dispatch.send_text_to_live_session(
        db=db,
        owner_id=owner_id,
        session=source_session,
        text=message,
        request_id=request_id,
        timeout_secs=15,
    )

    if not send_result.ok:
        error_code = _managed_local_send_failure_code(send_result)
        run_best_effort_session_turn_write(
            db_bind=db.get_bind(),
            label="send_failed",
            fn=lambda turn_db: mark_session_turn_failed(
                turn_db,
                session_id=source_session.id,
                request_id=request_id,
                error_code=error_code,
            ),
        )
        error_message = str(send_result.error or "Failed to send text to managed local session")
        logger.warning(
            "[%s] Managed-local stream send failed for %s: error_code=%s verified=%s exit_code=%s error=%s",
            request_id,
            source_session.id,
            error_code,
            bool(getattr(send_result, "verified_turn_started", False)),
            getattr(send_result, "exit_code", None),
            error_message,
        )
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
    run_best_effort_session_turn_write(
        db_bind=db.get_bind(),
        label="send_accepted",
        fn=lambda turn_db: mark_session_turn_send_accepted(
            turn_db,
            session_id=source_session.id,
            request_id=request_id,
        ),
    )

    terminal_task = asyncio.create_task(
        await_managed_local_turn_terminal(
            db_bind=db.get_bind(),
            session_id=source_session.id,
            after_observation_id=baseline_hook_observation_id,
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
        run_best_effort_session_turn_write(
            db_bind=db.get_bind(),
            label="terminal",
            fn=lambda turn_db: mark_session_turn_terminal(
                turn_db,
                session_id=source_session.id,
                request_id=request_id,
                phase=terminal_result.phase,
                terminal_at=terminal_result.occurred_at,
            ),
        )
    if new_events:
        run_best_effort_session_turn_write(
            db_bind=db.get_bind(),
            label="durable",
            fn=lambda turn_db: maybe_mark_session_turn_durable(
                turn_db,
                session_id=source_session.id,
            ),
        )
    turn_snapshot = get_session_turn_snapshot_best_effort(
        db_bind=db.get_bind(),
        session_id=source_session.id,
        request_id=request_id,
    )
    if not new_events:
        ledger_snapshot, ledger_events = hydrate_turn_events_from_snapshot(
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
        run_best_effort_session_turn_write(
            db_bind=db.get_bind(),
            label="turn_timeout",
            fn=lambda turn_db: mark_session_turn_failed(
                turn_db,
                session_id=source_session.id,
                request_id=request_id,
                error_code=SESSION_TURN_ERROR_TURN_TIMEOUT,
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
