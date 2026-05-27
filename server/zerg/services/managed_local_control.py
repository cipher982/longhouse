"""Shared managed-local control helpers.

This module keeps transport-aware local session control in one place so the
session-chat route and Loop actions use the same managed-local semantics.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_INTERRUPT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_STEER_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import MISSING_LEGACY_RUNNER_METADATA_ERROR
from zerg.services.managed_control_dispatcher import dispatch_managed_control_command
from zerg.services.managed_control_dispatcher import select_managed_control_transport
from zerg.services.managed_local_transport import ManagedLocalTransportError
from zerg.services.managed_local_transport import build_managed_local_interrupt_command
from zerg.services.managed_local_transport import build_managed_local_send_text_command
from zerg.services.managed_local_transport import build_managed_local_steer_text_command
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_runtime import runtime_event_from_observation
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

MANAGED_LOCAL_EVENT_TIMEOUT_SECS = 150.0
MANAGED_LOCAL_POLL_INTERVAL_SECS = 1.0
MANAGED_LOCAL_STABLE_POLLS = 1
MANAGED_LOCAL_SYNC_STATUS_PENDING = "pending"
MANAGED_LOCAL_SYNC_STATUS_COMPLETE = "complete"
MANAGED_LOCAL_SYNC_STATUS_FAILED = "failed"
MANAGED_LOCAL_CONTROL_STATUS_COMPLETED = "completed"
MANAGED_LOCAL_CONTROL_STATUS_NEEDS_USER = "needs_user"
MANAGED_LOCAL_CONTROL_STATUS_BLOCKED = "blocked"
MANAGED_LOCAL_CONTROL_STATUS_FAILED = "failed"
_MANAGED_LOCAL_HOOK_RUNTIME_SOURCE = "claude_hook"
_MANAGED_LOCAL_RUNTIME_PHASE_SOURCES = frozenset(
    {
        _MANAGED_LOCAL_HOOK_RUNTIME_SOURCE,
        "codex_bridge",
    }
)
_MANAGED_LOCAL_ACTIVE_HOOK_PHASES = frozenset({"thinking", "running"})
_MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS = {
    "idle": MANAGED_LOCAL_CONTROL_STATUS_COMPLETED,
    "needs_user": MANAGED_LOCAL_CONTROL_STATUS_NEEDS_USER,
    "blocked": MANAGED_LOCAL_CONTROL_STATUS_BLOCKED,
}


@dataclass(frozen=True)
class ManagedLocalSendResult:
    ok: bool
    exit_code: int | None = None
    error: str | None = None
    baseline_event_id: int | None = None
    verified_turn_started: bool = False
    verified_user_event_id: int | None = None


@dataclass(frozen=True)
class ManagedLocalInterruptResult:
    ok: bool
    exit_code: int | None = None
    error: str | None = None
    stdout: str | None = None
    stderr: str | None = None


@dataclass(frozen=True)
class ManagedLocalPhaseUpdate:
    phase: str
    observation_id: int = 0
    occurred_at: datetime | None = None
    source: str = _MANAGED_LOCAL_HOOK_RUNTIME_SOURCE


@dataclass(frozen=True)
class ManagedLocalTerminalResult:
    phase: str
    control_status: str
    observation_id: int
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class _ManagedLocalHookObservation:
    observation_id: int
    phase: str | None
    occurred_at: datetime | None
    source: str


def _managed_control_transport_error(
    session: AgentSession,
    *,
    owner_id: int,
    command_type: str,
) -> str | None:
    if select_managed_control_transport(session, owner_id=owner_id, command_type=command_type) is None:
        return MISSING_LEGACY_RUNNER_METADATA_ERROR
    return None


def get_managed_local_control_status_for_phase(phase: str | None) -> str:
    normalized = str(phase or "").strip().lower()
    return _MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS.get(normalized, MANAGED_LOCAL_CONTROL_STATUS_COMPLETED)


def validate_managed_local_chat_done_payload(
    *,
    session_id: str,
    done_payload: Mapping[str, object] | None,
) -> str | None:
    """Validate the `/api/sessions/{id}/send-live` done payload for managed-local sends."""

    if done_payload is None:
        return "missing done payload"
    if done_payload.get("created_branch") is not False:
        return f"expected created_branch=false, got {done_payload.get('created_branch')!r}"
    if str(done_payload.get("shipped_session_id") or "") != session_id:
        return f"expected shipped_session_id={session_id}, got {done_payload.get('shipped_session_id')!r}"
    sync_status = str(done_payload.get("sync_status") or "").strip().lower()
    if sync_status not in {
        MANAGED_LOCAL_SYNC_STATUS_PENDING,
        MANAGED_LOCAL_SYNC_STATUS_COMPLETE,
    }:
        return f"expected sync_status in {{'pending','complete'}}, got {done_payload.get('sync_status')!r}"
    if done_payload.get("persistence_error") is not None:
        return f"unexpected persistence_error={done_payload.get('persistence_error')!r}"
    if sync_status == MANAGED_LOCAL_SYNC_STATUS_COMPLETE:
        if int(done_payload.get("persisted_events") or 0) <= 0:
            return f"expected persisted_events>0, got {done_payload.get('persisted_events')!r}"

    exit_code_raw = done_payload.get("exit_code")
    try:
        exit_code = int(exit_code_raw)
    except (TypeError, ValueError):
        return f"expected exit_code=0, got {exit_code_raw!r}"
    if exit_code != 0:
        return f"expected exit_code=0, got {done_payload.get('exit_code')!r}"
    return None


def get_managed_local_latest_event_id(*, db: Session, session_id: UUID) -> int:
    """Return the latest stored event id for a managed-local session."""
    return int(AgentsStore(db).get_latest_event_id(session_id) or 0)


def get_managed_local_latest_hook_observation_id(*, db: Session, session_id: UUID) -> int:
    """Return the latest hook-driven runtime observation cursor for a managed-local session."""
    row = (
        db.query(SessionObservation.id)
        .filter(
            SessionObservation.session_id == session_id,
            SessionObservation.source.in_(tuple(_MANAGED_LOCAL_RUNTIME_PHASE_SOURCES)),
            SessionObservation.kind == OBS_KIND_RUNTIME_SIGNAL,
        )
        .order_by(SessionObservation.id.desc())
        .first()
    )
    return int(row[0]) if row else 0


def _fetch_managed_local_events_since(*, db_bind, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id > after_event_id)
            .filter(durable_transcript_event_predicate())
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


def _fetch_managed_local_hook_observations_since(
    *,
    db_bind,
    session_id: UUID,
    after_observation_id: int,
) -> list[_ManagedLocalHookObservation]:
    with Session(bind=db_bind) as poll_db:
        observations = (
            poll_db.query(SessionObservation)
            .filter(
                SessionObservation.session_id == session_id,
                SessionObservation.source.in_(tuple(_MANAGED_LOCAL_RUNTIME_PHASE_SOURCES)),
                SessionObservation.kind == OBS_KIND_RUNTIME_SIGNAL,
                SessionObservation.id > after_observation_id,
            )
            .order_by(SessionObservation.id.asc())
            .all()
        )
    hook_observations: list[_ManagedLocalHookObservation] = []
    for observation in observations:
        try:
            runtime_event = runtime_event_from_observation(observation)
        except ValueError:
            logger.warning(
                "Skipping malformed managed-local hook observation %s",
                getattr(observation, "observation_id", None),
            )
            continue
        if runtime_event is None or runtime_event.kind != "phase_signal":
            continue
        hook_observations.append(
            _ManagedLocalHookObservation(
                observation_id=int(getattr(observation, "id", 0) or 0),
                phase=runtime_event.phase,
                occurred_at=normalize_utc(runtime_event.occurred_at),
                source=runtime_event.source,
            )
        )
    return hook_observations


def _load_managed_local_runtime_state(*, db_bind, session_id: UUID) -> SessionRuntimeState | None:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(SessionRuntimeState)
            .filter(SessionRuntimeState.session_id == session_id)
            .order_by(SessionRuntimeState.updated_at.desc(), SessionRuntimeState.runtime_version.desc())
            .first()
        )


def _hook_observation_matches_canonical_state(
    *,
    db_bind,
    session_id: UUID,
    observation: _ManagedLocalHookObservation,
) -> bool:
    state = _load_managed_local_runtime_state(db_bind=db_bind, session_id=session_id)
    if state is None:
        return False
    if str(getattr(state, "phase", "") or "").strip() != str(getattr(observation, "phase", "") or "").strip():
        return False
    state_signal_at = normalize_utc(getattr(state, "last_runtime_signal_at", None))
    observation_occurred_at = normalize_utc(getattr(observation, "occurred_at", None))
    return state_signal_at == observation_occurred_at


async def await_managed_local_hook_phase_update(
    *,
    db_bind,
    session_id: UUID,
    after_observation_id: int,
    phases: set[str] | frozenset[str] | None = None,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> ManagedLocalPhaseUpdate | None:
    """Wait for a new hook-driven runtime phase after the provided cursor.

    The `/api/agents/presence` endpoint records hook runtime signals as
    SessionObservation facts, so this polls the raw observation cursor and
    verifies the canonical SessionRuntimeState reducer output before returning.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    cursor = after_observation_id

    while loop.time() < deadline:
        hook_observations = _fetch_managed_local_hook_observations_since(
            db_bind=db_bind,
            session_id=session_id,
            after_observation_id=cursor,
        )
        for observation in hook_observations:
            cursor = max(cursor, int(getattr(observation, "observation_id", 0) or 0))
            phase = str(getattr(observation, "phase", "") or "").strip()
            if not _hook_observation_matches_canonical_state(
                db_bind=db_bind,
                session_id=session_id,
                observation=observation,
            ):
                continue
            if phases is None or phase in phases:
                return ManagedLocalPhaseUpdate(
                    phase=phase,
                    observation_id=int(getattr(observation, "observation_id", 0) or 0),
                    occurred_at=normalize_utc(getattr(observation, "occurred_at", None)),
                    source=str(getattr(observation, "source", "") or _MANAGED_LOCAL_HOOK_RUNTIME_SOURCE),
                )
        await asyncio.sleep(poll_interval_secs)

    return None


async def await_managed_local_turn_terminal(
    *,
    db_bind,
    session_id: UUID,
    after_observation_id: int,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> ManagedLocalTerminalResult | None:
    """Wait for a new terminal phase for a managed-local turn.

    Polls SessionObservation rows keyed by the session's hook observation
    cursor. Callers pass a pre-send cursor, so any newer idle/needs_user/
    blocked observation still belongs to the in-flight managed-local turn.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    cursor = after_observation_id

    while loop.time() < deadline:
        hook_observations = _fetch_managed_local_hook_observations_since(
            db_bind=db_bind,
            session_id=session_id,
            after_observation_id=cursor,
        )
        for observation in hook_observations:
            cursor = max(cursor, int(getattr(observation, "observation_id", 0) or 0))
            phase = str(getattr(observation, "phase", "") or "").strip()
            if not _hook_observation_matches_canonical_state(
                db_bind=db_bind,
                session_id=session_id,
                observation=observation,
            ):
                continue
            if phase in _MANAGED_LOCAL_ACTIVE_HOOK_PHASES:
                continue
            if phase not in _MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS:
                continue
            return ManagedLocalTerminalResult(
                phase=phase,
                control_status=_MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS.get(
                    phase,
                    MANAGED_LOCAL_CONTROL_STATUS_COMPLETED,
                ),
                observation_id=int(getattr(observation, "observation_id", 0) or 0),
                occurred_at=getattr(observation, "occurred_at", None),
            )
        await asyncio.sleep(poll_interval_secs)

    return None


async def await_managed_local_turn_events(
    *,
    db_bind,
    session_id: UUID,
    after_event_id: int,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> list[AgentEvent]:
    """Wait until a managed-local send produces persisted timeline events."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    latest_seen = after_event_id
    stable_polls = 0

    while loop.time() < deadline:
        with Session(bind=db_bind) as poll_db:
            latest_event_id = get_managed_local_latest_event_id(db=poll_db, session_id=session_id)
        if latest_event_id > after_event_id:
            if latest_event_id == latest_seen:
                stable_polls += 1
            else:
                latest_seen = latest_event_id
                stable_polls = 0

            if stable_polls >= MANAGED_LOCAL_STABLE_POLLS:
                return _fetch_managed_local_events_since(
                    db_bind=db_bind,
                    session_id=session_id,
                    after_event_id=after_event_id,
                )

        await asyncio.sleep(poll_interval_secs)

    return []


def _managed_local_events_include_expected_user_prompt(
    *,
    events: list[AgentEvent],
    expected_user_text: str,
) -> bool:
    expected = str(expected_user_text or "")
    if not expected:
        return False
    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        if role != "user":
            continue
        content_text = str(getattr(event, "content_text", "") or "")
        if strip_claude_channel_wrapper(content_text) == expected:
            return True
    return False


async def await_managed_local_persisted_user_prompt(
    *,
    db_bind,
    session_id: UUID,
    after_event_id: int,
    expected_user_text: str,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> AgentEvent | None:
    """Wait until the injected user prompt is durably visible in managed-local events."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    latest_seen = after_event_id
    stable_polls = 0

    while loop.time() < deadline:
        with Session(bind=db_bind) as poll_db:
            latest_event_id = int(get_managed_local_latest_event_id(db=poll_db, session_id=session_id) or 0)
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
                if not _managed_local_events_include_expected_user_prompt(
                    events=events,
                    expected_user_text=expected_user_text,
                ):
                    await asyncio.sleep(poll_interval_secs)
                    continue
                for event in events:
                    role = str(getattr(event, "role", "") or "").strip().lower()
                    if role != "user":
                        continue
                    content_text = str(getattr(event, "content_text", "") or "")
                    if strip_claude_channel_wrapper(content_text) == str(expected_user_text or ""):
                        return event

        await asyncio.sleep(poll_interval_secs)

    return None


async def interrupt_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    commis_id: str | None = None,
    timeout_secs: int = 15,
) -> ManagedLocalInterruptResult:
    """Dispatch an interrupt request for the active managed-local turn.

    This is an explicit operator recovery primitive. It dispatches through the
    same runner/transport seam as live-send so callers do not need to know
    whether the session is backed by Codex app-server or Claude channels. A
    successful result means the interrupt command ran, not that the provider
    has confirmed the turn stopped.
    """

    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalInterruptResult(ok=False, error="Session is not managed_local")
    transport_error = _managed_control_transport_error(
        session,
        owner_id=owner_id,
        command_type=MANAGED_CONTROL_COMMAND_INTERRUPT,
    )
    if transport_error is not None:
        return ManagedLocalInterruptResult(ok=False, error=transport_error)

    try:
        command = build_managed_local_interrupt_command(session=session)
    except ManagedLocalTransportError as exc:
        return ManagedLocalInterruptResult(ok=False, error=str(exc))

    result = await dispatch_managed_control_command(
        db=db,
        owner_id=owner_id,
        session=session,
        command=command,
        timeout_secs=timeout_secs,
        command_type=MANAGED_CONTROL_COMMAND_INTERRUPT,
        payload={},
        commis_id=commis_id,
        run_id=None,
        failure_message="Failed to dispatch interrupt command",
    )
    if not result.ok:
        return ManagedLocalInterruptResult(
            ok=False,
            error=result.error or "Failed to dispatch interrupt command",
        )

    data = result.data or {}
    exit_code = int(data.get("exit_code", 1))
    stdout = data.get("stdout") or ""
    stderr = data.get("stderr") or ""
    if exit_code != 0:
        detail = stderr.strip() or stdout.strip()
        return ManagedLocalInterruptResult(
            ok=False,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            error=detail or "Managed local interrupt command failed",
        )
    return ManagedLocalInterruptResult(ok=True, exit_code=0, stdout=stdout, stderr=stderr)


async def send_text_to_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 15,
    verify_turn_started: bool = False,
    verification_timeout_secs: float | None = None,
    attachments: list[dict] | None = None,
) -> ManagedLocalSendResult:
    """Send text into a managed-local session via its configured transport.

    Returns a normalized result so callers do not need to know the runner
    dispatch envelope details.
    """

    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalSendResult(ok=False, error="Session is not managed_local")
    transport_error = _managed_control_transport_error(
        session,
        owner_id=owner_id,
        command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
    )
    if transport_error is not None:
        return ManagedLocalSendResult(ok=False, error=transport_error)

    transport = str(getattr(session, "managed_transport", "") or "").strip()
    effective_verify = bool(verify_turn_started)

    baseline_event_id = get_managed_local_latest_event_id(db=db, session_id=session.id)
    baseline_hook_observation_id = (
        get_managed_local_latest_hook_observation_id(db=db, session_id=session.id)
        if effective_verify and transport != ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value
        else 0
    )
    try:
        command = build_managed_local_send_text_command(
            session=session,
            text=text,
            attachments=attachments,
        )
    except ManagedLocalTransportError as exc:
        return ManagedLocalSendResult(ok=False, error=str(exc))
    result = await dispatch_managed_control_command(
        db=db,
        owner_id=owner_id,
        session=session,
        command=command,
        timeout_secs=timeout_secs,
        command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        payload={"text": text},
        commis_id=commis_id,
        run_id=None,
        failure_message="Failed to send text to managed local session",
    )

    if not result.ok:
        return ManagedLocalSendResult(
            ok=False,
            baseline_event_id=baseline_event_id,
            error=result.error or "Failed to send text to managed local session",
        )

    data = result.data or {}
    exit_code = int(data.get("exit_code", 1))
    if exit_code != 0:
        detail = (data.get("stderr") or "").strip() or (data.get("stdout") or "").strip()
        return ManagedLocalSendResult(
            ok=False,
            exit_code=exit_code,
            baseline_event_id=baseline_event_id,
            error=detail or "Managed local send-text command failed",
        )

    if effective_verify:
        if result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL:
            turn_id = str(data.get("turn_id") or "").strip()
            if turn_id:
                return ManagedLocalSendResult(
                    ok=True,
                    exit_code=0,
                    baseline_event_id=baseline_event_id,
                    verified_turn_started=True,
                )
        verification_timeout = float(
            verification_timeout_secs if verification_timeout_secs is not None else MANAGED_LOCAL_EVENT_TIMEOUT_SECS
        )
        if transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value:
            persisted_prompt = await await_managed_local_persisted_user_prompt(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_event_id=baseline_event_id,
                expected_user_text=text,
                timeout_secs=verification_timeout,
            )
            if persisted_prompt is None:
                return ManagedLocalSendResult(
                    ok=False,
                    exit_code=0,
                    baseline_event_id=baseline_event_id,
                    error="Managed local session did not acknowledge the prompt after send",
                    verified_turn_started=False,
                )
            return ManagedLocalSendResult(
                ok=True,
                exit_code=0,
                baseline_event_id=baseline_event_id,
                verified_turn_started=True,
                verified_user_event_id=int(getattr(persisted_prompt, "id", 0) or 0) or None,
            )
        else:
            hook_event = await await_managed_local_hook_phase_update(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_observation_id=baseline_hook_observation_id,
                phases=set(_MANAGED_LOCAL_ACTIVE_HOOK_PHASES),
                timeout_secs=verification_timeout,
            )
            if hook_event is None:
                return ManagedLocalSendResult(
                    ok=False,
                    exit_code=0,
                    baseline_event_id=baseline_event_id,
                    error="Managed local session did not acknowledge the prompt after send",
                    verified_turn_started=False,
                )

    return ManagedLocalSendResult(
        ok=True,
        exit_code=0,
        baseline_event_id=baseline_event_id,
        verified_turn_started=effective_verify,
    )


# Sentinel returned in `ManagedLocalSendResult.error` when the codex-bridge
# CLI reports `error_code: turn_ended` on stderr. Backend dispatch turns this
# into a 409 with a stable error code so the UI can prompt the user to queue
# instead.
MANAGED_LOCAL_STEER_TURN_ENDED = "turn_ended"


async def steer_text_to_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 15,
    attachments: list[dict] | None = None,
) -> ManagedLocalSendResult:
    """Inject mid-turn steer text into the currently active managed turn.

    Codex app-server and Claude channel bridge both support live injection.
    The transport helper raises for process-only observe transports. Callers
    should gate on `can_steer_active_turn`.

    Turn-ended races (active turn ended between the UI's capability check
    and this dispatch) surface as `ManagedLocalSendResult(ok=False,
    error=MANAGED_LOCAL_STEER_TURN_ENDED)` so the router can map to a
    structured 409.
    """

    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalSendResult(ok=False, error="Session is not managed_local")
    transport_error = _managed_control_transport_error(
        session,
        owner_id=owner_id,
        command_type=MANAGED_CONTROL_COMMAND_STEER_TEXT,
    )
    if transport_error is not None:
        return ManagedLocalSendResult(ok=False, error=transport_error)

    try:
        command = build_managed_local_steer_text_command(
            session=session,
            text=text,
            attachments=attachments,
        )
    except ManagedLocalTransportError as exc:
        return ManagedLocalSendResult(ok=False, error=str(exc))

    result = await dispatch_managed_control_command(
        db=db,
        owner_id=owner_id,
        session=session,
        command=command,
        timeout_secs=timeout_secs,
        command_type=MANAGED_CONTROL_COMMAND_STEER_TEXT,
        payload={"text": text, "intent": "steer"},
        commis_id=commis_id,
        run_id=None,
        failure_message="Failed to dispatch steer command",
    )
    if not result.ok:
        return ManagedLocalSendResult(
            ok=False,
            error=result.error or "Failed to dispatch steer command",
        )

    data = result.data or {}
    exit_code = int(data.get("exit_code", 1))
    stderr = data.get("stderr") or ""
    if exit_code == 2 and "error_code: turn_ended" in stderr:
        return ManagedLocalSendResult(
            ok=False,
            exit_code=exit_code,
            error=MANAGED_LOCAL_STEER_TURN_ENDED,
        )
    if exit_code != 0:
        detail = stderr.strip() or (data.get("stdout") or "").strip()
        return ManagedLocalSendResult(
            ok=False,
            exit_code=exit_code,
            error=detail or "Managed local steer command failed",
        )
    return ManagedLocalSendResult(ok=True, exit_code=0)
