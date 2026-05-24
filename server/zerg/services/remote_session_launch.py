"""Remote session launch — POST /api/sessions/launch.

See docs/specs/remote-session-launch.md. The caller (user-auth'd browser or
iOS) picks a target machine + cwd + provider. We verify ownership, confirm
the Machine Agent is connected, pre-allocate the session UUID, insert the
``sessions`` row in ``launch_state=launching``, dispatch the ``session.launch``
command over the existing control WebSocket, and reconcile the row based on
the typed result.

Control-channel contract preserved: every command carries ``session_id``,
including launch. The session id is the one we pre-allocated. No parallel
``launch_requests`` table exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.device_token import DeviceToken
from zerg.services.agents.kernel_writes import ensure_open_run_for_session
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_launch_attempt
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.agents.kernel_writes import update_launch_attempt
from zerg.services.agents.kernel_writes import upsert_connection_for_run
from zerg.services.machine_control_channel import MachineControlChannelRegistry
from zerg.services.machine_control_channel import MachineControlCommandResponse
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_loop_mode import SessionLoopMode

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"codex"}
LAUNCH_COMMAND_TIMEOUT_SECS = 30
LAUNCH_LEASE_SECS = 120


class RemoteLaunchError(RuntimeError):
    """Expected remote-launch failure with user-facing detail."""

    def __init__(self, detail: str, code: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RemoteLaunchParams:
    owner_id: int
    device_id: str
    provider: str
    cwd: str
    git_repo: str | None = None
    git_branch: str | None = None
    project: str | None = None
    display_name: str | None = None
    client_request_id: str | None = None


@dataclass(frozen=True)
class RemoteLaunchResult:
    session_id: UUID
    launch_state: str
    launch_error_code: str | None = None
    launch_error_message: str | None = None


def _verify_device_owned_by(db: Session, *, owner_id: int, device_id: str) -> None:
    """Require that ``device_id`` has a non-revoked device token for ``owner_id``."""
    q = (
        db.query(DeviceToken.id)
        .filter(DeviceToken.owner_id == owner_id)
        .filter(DeviceToken.device_id == device_id)
        .filter(DeviceToken.revoked_at.is_(None))
        .first()
    )
    if q is None:
        raise RemoteLaunchError(
            f"Device {device_id!r} is not enrolled for this user",
            code="device_not_enrolled",
            status_code=404,
        )


def _project_for(cwd: str, project: str | None) -> str:
    if project and project.strip():
        return project.strip()
    return Path(cwd).name or "managed-local"


def _launch_state_for_attempt(attempt: SessionLaunchAttempt) -> str:
    state = str(attempt.state or "").strip()
    if state == "failed":
        return "launch_failed"
    if state == "abandoned":
        return "launch_orphaned"
    if attempt.run_id is not None or state == "adopted":
        return "live"
    if state == "dispatched":
        return "launching_unknown"
    return "launching"


def _launch_result_for_attempt(attempt: SessionLaunchAttempt) -> RemoteLaunchResult:
    return RemoteLaunchResult(
        session_id=UUID(str(attempt.session_id)),
        launch_state=_launch_state_for_attempt(attempt),
        launch_error_code=attempt.error_code,
        launch_error_message=attempt.error_message,
    )


def _control_plane_for_provider(provider: str | None) -> str:
    return (
        "codex_bridge"
        if provider == "codex"
        else "opencode_process"
        if provider == "opencode"
        else "antigravity_process"
        if provider == "antigravity"
        else "claude_channel_bridge"
    )


def _attach_live_launch_run(
    db: Session,
    *,
    session: AgentSession,
    attempt: SessionLaunchAttempt,
    external_name: str | None,
) -> None:
    run = ensure_open_run_for_session(
        db,
        session,
        launch_origin="longhouse_spawned",
        host_id=session.device_id,
    )
    upsert_connection_for_run(
        db,
        run=run,
        control_plane=_control_plane_for_provider(session.provider),
        acquisition_kind="spawned_control",
        state="attached",
        external_name=external_name or session.device_id,
        can_send_input=1,
        can_interrupt=1,
        can_terminate=1,
        can_tail_output=1,
        can_resume=1,
    )
    update_launch_attempt(
        db,
        attempt,
        state="adopted",
        run=run,
        clear_expires=True,
    )


async def launch_remote_session(
    db: Session,
    params: RemoteLaunchParams,
    *,
    registry: MachineControlChannelRegistry | None = None,
) -> RemoteLaunchResult:
    provider = (params.provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise RemoteLaunchError(
            f"provider {provider!r} is not supported for remote launch in v1",
            code="provider_unsupported",
            status_code=400,
        )

    device_id = (params.device_id or "").strip()
    if not device_id:
        raise RemoteLaunchError(
            "device_id is required",
            code="invalid_request",
            status_code=400,
        )
    cwd = (params.cwd or "").strip()
    if not cwd:
        raise RemoteLaunchError(
            "cwd is required",
            code="invalid_request",
            status_code=400,
        )
    if not cwd.startswith("/"):
        raise RemoteLaunchError(
            "cwd must be absolute",
            code="cwd_not_allowed",
            status_code=400,
        )

    _verify_device_owned_by(db, owner_id=params.owner_id, device_id=device_id)

    client_request_id = (params.client_request_id or "").strip() or None
    if client_request_id:
        existing = (
            db.query(SessionLaunchAttempt)
            .join(AgentSession, AgentSession.id == SessionLaunchAttempt.session_id)
            .filter(SessionLaunchAttempt.client_request_id == client_request_id)
            .filter(SessionLaunchAttempt.owner_id == params.owner_id)
            .filter(SessionLaunchAttempt.host_id == device_id)
            .filter(SessionLaunchAttempt.provider == provider)
            .filter(AgentSession.device_id == device_id)
            .filter(AgentSession.provider == provider)
            .order_by(SessionLaunchAttempt.created_at.desc(), SessionLaunchAttempt.id.desc())
            .first()
        )
        if existing is not None:
            return _launch_result_for_attempt(existing)

    reg = registry or get_machine_control_channel_registry()
    info = reg.info(owner_id=params.owner_id, device_id=device_id)
    if info is None:
        raise RemoteLaunchError(
            f"Machine {device_id!r} is offline",
            code="machine_offline",
            status_code=409,
        )
    launch_cap = f"{provider}.launch"
    if launch_cap not in info.supports:
        raise RemoteLaunchError(
            f"Machine {device_id!r} does not support {launch_cap}",
            code="provider_unsupported",
            status_code=409,
        )

    session_uuid = uuid4()
    command_id = f"launch-{session_uuid}"
    project = _project_for(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=LAUNCH_LEASE_SECS)

    session = AgentSession(
        id=session_uuid,
        provider=provider,
        environment="development",
        project=project,
        device_id=device_id,
        device_name=info.machine_name or device_id,
        cwd=cwd,
        git_repo=params.git_repo,
        git_branch=params.git_branch,
        started_at=now,
        ended_at=None,
        provider_session_id=str(session_uuid),
        thread_root_session_id=session_uuid,
        continued_from_session_id=None,
        continuation_kind="local",
        origin_label=info.machine_name or device_id,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode=SessionLoopMode.ASSIST.value,
        execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
        managed_transport=ManagedSessionTransport.for_provider(provider).value,
        source_runner_id=None,
        source_runner_name=info.machine_name or device_id,
        managed_session_name=display_name,
        launch_state="launching",
        launch_lease_until=lease_until,
        launch_command_id=command_id,
        launch_client_request_id=client_request_id,
    )
    db.add(session)
    db.flush()

    # Phase 2 dual-write: materialize kernel rows alongside legacy launch_*.
    primary_thread = ensure_primary_thread(db, session)
    record_thread_alias(
        db,
        thread=primary_thread,
        provider=provider,
        alias_kind="provider_session_id",
        alias_value=str(session_uuid),
    )
    launch_attempt = record_launch_attempt(
        db,
        session=session,
        thread=primary_thread,
        provider=provider,
        host_id=device_id,
        owner_id=params.owner_id,
        client_request_id=client_request_id,
        command_id=command_id,
        state="pending",
        expires_at=lease_until,
    )
    db.commit()
    db.refresh(session)

    payload = {
        "provider": provider,
        "cwd": cwd,
        "git_repo": params.git_repo,
        "git_branch": params.git_branch,
        "project": project,
        "display_name": display_name,
    }
    response: MachineControlCommandResponse = await reg.send_command(
        owner_id=params.owner_id,
        device_id=device_id,
        session_id=str(session_uuid),
        command_type="session.launch",
        payload=payload,
        timeout_secs=LAUNCH_COMMAND_TIMEOUT_SECS,
        command_id=command_id,
    )

    if not response.transport_ok:
        # Timeout or transport error — the command may already be on the wire.
        # Mark dispatched so the reaper recognizes it as in-flight; the late
        # reconcile path will flip to failed/abandoned with the real outcome.
        error_message = response.error or "control channel transport failed"
        update_launch_attempt(
            db,
            launch_attempt,
            state="dispatched",
            error_message=error_message,
        )
        db.commit()
        db.refresh(session)
        return RemoteLaunchResult(
            session_id=session_uuid,
            launch_state="launching_unknown",
            launch_error_code=None,
            launch_error_message=error_message,
        )

    message = response.message or {}
    if message.get("ok"):
        _attach_live_launch_run(
            db,
            session=session,
            attempt=launch_attempt,
            external_name=info.machine_name or device_id,
        )
        db.commit()
        db.refresh(session)
        elapsed_ms = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)
        logger.info(
            "remote_launch session=%s device=%s provider=%s state=live duration_ms=%s",
            session_uuid,
            device_id,
            provider,
            elapsed_ms,
        )
        return RemoteLaunchResult(session_id=session_uuid, launch_state="live")

    error = message.get("error") or {}
    code = str(error.get("code") or "provider_launch_failed")
    err_msg = str(error.get("message") or "unknown error")
    session.ended_at = datetime.now(timezone.utc)
    update_launch_attempt(
        db,
        launch_attempt,
        state="failed",
        error_code=code,
        error_message=err_msg,
        clear_expires=True,
    )
    db.commit()
    db.refresh(session)
    logger.warning(
        "remote_launch session=%s device=%s provider=%s state=launch_failed code=%s",
        session_uuid,
        device_id,
        provider,
        code,
    )
    return RemoteLaunchResult(
        session_id=session_uuid,
        launch_state="launch_failed",
        launch_error_code=code,
        launch_error_message=err_msg,
    )


def reconcile_launch_from_command_result(db: Session, message: dict) -> bool:
    """Late-result reconciliation for a ``session.launch`` command.

    Called from the control-channel WS handler whenever a ``command_result``
    frame arrives that the in-memory registry did not map to an in-flight
    request. If the frame belongs to a launch we dispatched earlier (matched
    by ``launch_command_id``), flip the stored row from ``launching_unknown``
    to the right terminal state.

    Returns True if a row was reconciled.
    """
    command_id = str(message.get("command_id") or "").strip()
    if not command_id or not command_id.startswith("launch-"):
        return False
    attempt = db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.command_id == command_id).first()
    if attempt is None:
        return False
    if attempt.run_id is not None or attempt.state == "adopted":
        return True
    if attempt.state not in {"pending", "dispatched"}:
        return False
    session = db.query(AgentSession).filter(AgentSession.id == attempt.session_id).first()
    if session is None:
        return False
    # Defense-in-depth: if the Machine Agent reported back a session_id, it
    # must match the one we pre-allocated. Mismatch means the command_id
    # was reused or rebound — refuse to mutate state on the wrong row.
    reported_session_id = str(message.get("session_id") or "").strip()
    if reported_session_id and reported_session_id != str(session.id):
        return False

    if message.get("ok"):
        _attach_live_launch_run(
            db,
            session=session,
            attempt=attempt,
            external_name=session.device_name or session.device_id,
        )
    else:
        error = message.get("error") or {}
        if session.ended_at is None:
            session.ended_at = datetime.now(timezone.utc)
        update_launch_attempt(
            db,
            attempt,
            state="failed",
            error_code=str(error.get("code") or "provider_launch_failed"),
            error_message=str(error.get("message") or "unknown error"),
            clear_expires=True,
        )
    db.commit()
    return True


def reap_orphaned_launches(db: Session, *, now: datetime | None = None) -> int:
    """Move expired pending/dispatched launch attempts to abandoned.

    Returns the number of rows reaped. Intended to be called on a low-frequency
    tick (every 30-60s) from a background task.
    """
    cutoff = now or datetime.now(timezone.utc)
    stale = (
        db.query(SessionLaunchAttempt)
        .filter(SessionLaunchAttempt.state.in_(["pending", "dispatched"]))
        .filter(SessionLaunchAttempt.expires_at.is_not(None))
        .filter(SessionLaunchAttempt.expires_at <= cutoff)
        .all()
    )

    for attempt in stale:
        session = db.query(AgentSession).filter(AgentSession.id == attempt.session_id).first()
        if session is not None and session.ended_at is None:
            session.ended_at = cutoff
        update_launch_attempt(
            db,
            attempt,
            state="abandoned",
            error_code=attempt.error_code or "launch_timeout",
            error_message=attempt.error_message or "Machine Agent did not report back before lease expired",
            clear_expires=True,
        )
    if stale:
        db.commit()
    return len(stale)


__all__ = [
    "LAUNCH_COMMAND_TIMEOUT_SECS",
    "LAUNCH_LEASE_SECS",
    "RemoteLaunchError",
    "RemoteLaunchParams",
    "RemoteLaunchResult",
    "launch_remote_session",
    "reap_orphaned_launches",
    "reconcile_launch_from_command_result",
]
