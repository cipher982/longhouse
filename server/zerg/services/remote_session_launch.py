"""Remote session launch — POST /api/sessions/launch.

See docs/specs/remote-session-launch.md. The caller (user-auth'd browser or
iOS) picks a target machine + cwd + provider. We verify ownership, confirm
the Machine Agent is connected, pre-allocate the session UUID, record a
``SessionLaunchAttempt(state=pending)``, dispatch the ``session.launch`` command
over the existing control WebSocket, and reconcile the attempt based on the
typed result.

Control-channel contract preserved: every command carries ``session_id``,
including launch. The session id is the one we pre-allocated. No parallel
``launch_requests`` table exists.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.device_token import DeviceToken
from zerg.models.live_store import LiveArchiveOutbox
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.agents.kernel_writes import ensure_open_run_for_session
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_launch_attempt
from zerg.services.agents.kernel_writes import record_run
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.agents.kernel_writes import update_launch_attempt
from zerg.services.agents.kernel_writes import upsert_connection_for_run
from zerg.services.live_archive_outbox import ONE_SHOT_CONTROL_PLANE_BY_PROVIDER
from zerg.services.live_archive_outbox import REMOTE_LAUNCH_KIND
from zerg.services.live_archive_outbox import REMOTE_LAUNCH_OUTCOME_KIND
from zerg.services.live_archive_outbox import enqueue_remote_launch_outbox
from zerg.services.live_archive_outbox import enqueue_remote_launch_outcome_outbox
from zerg.services.live_archive_outbox import remote_launch_idempotency_key
from zerg.services.live_launch_readiness import get_live_launch_readiness_by_client_request
from zerg.services.live_launch_readiness import update_live_launch_readiness_state
from zerg.services.live_launch_readiness import upsert_live_launch_readiness
from zerg.services.machine_control_channel import MachineControlChannelRegistry
from zerg.services.machine_control_channel import MachineControlCommandResponse
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_provider_contracts import continue_supported_providers
from zerg.services.managed_provider_contracts import control_plane_for_provider
from zerg.services.managed_provider_contracts import remote_launch_supported_providers
from zerg.services.managed_provider_contracts import require_contract_for_provider
from zerg.services.managed_provider_contracts import run_once_supported_providers
from zerg.services.session_continue_targets import resolve_native_continue_target
from zerg.services.session_kernel_projection import is_synthetic_provider_session_id
from zerg.services.session_launch_lifecycle import DEFAULT_REMOTE_EXECUTION_LIFETIME
from zerg.services.session_launch_lifecycle import RemoteExecutionLifetime
from zerg.services.session_launch_lifecycle import RemoteLaunchErrorCode
from zerg.services.session_launch_lifecycle import RemoteLaunchLifecycleState
from zerg.services.session_launch_lifecycle import normalize_remote_execution_lifetime
from zerg.services.session_launch_lifecycle import normalize_remote_launch_error_code
from zerg.services.session_launch_lifecycle import project_remote_launch_lifecycle
from zerg.services.write_serializer import get_live_write_serializer
from zerg.session_loop_mode import SessionLoopMode

logger = logging.getLogger(__name__)

LIVE_CONTROL_SUPPORTED_PROVIDERS = remote_launch_supported_providers()
RUN_ONCE_SUPPORTED_PROVIDERS = run_once_supported_providers()
LAUNCH_COMMAND_TIMEOUT_SECS = 30
LAUNCH_LEASE_SECS = 120


# One-shot (Console) runs record their SessionConnection against the engine's
# direct exec control plane, not the provider's primary contract control_plane
# (which for Helm providers like codex is the live bridge). codex_exec is
# intentionally not contract-registered; cursor_acp is cursor's one-shot / Console
# control plane (headless ACP over stdio) selected explicitly for run-once launches.
# cursor_helm is the live / Helm control plane (contract-registered, direct machine
# control plane) used by the `longhouse cursor` interactive launcher. cursor_exec is
# retained as a legacy alias. Keep this map explicit rather than deriving from the
# contract.
class RemoteLaunchError(RuntimeError):
    """Expected remote-launch failure with user-facing detail."""

    def __init__(self, detail: str, code: RemoteLaunchErrorCode, *, status_code: int = 400) -> None:
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
    initial_prompt: str | None = None
    execution_lifetime: RemoteExecutionLifetime = DEFAULT_REMOTE_EXECUTION_LIFETIME
    client_request_id: str | None = None


@dataclass(frozen=True)
class RemoteContinueParams:
    owner_id: int
    session_id: UUID
    client_request_id: str
    device_id: str | None = None
    cwd: str | None = None
    message: str | None = None
    execution_lifetime: RemoteExecutionLifetime = DEFAULT_REMOTE_EXECUTION_LIFETIME


@dataclass(frozen=True)
class RemoteLaunchResult:
    session_id: UUID
    launch_state: RemoteLaunchLifecycleState
    execution_lifetime: RemoteExecutionLifetime
    launch_error_code: RemoteLaunchErrorCode | None = None
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
    label = Path(cwd).name.strip()
    if label and label != "workspace":
        return label
    return "managed-local"


def _launch_result_for_attempt(attempt: SessionLaunchAttempt) -> RemoteLaunchResult:
    lifecycle = project_remote_launch_lifecycle(attempt)
    if lifecycle is None:  # pragma: no cover - defensive for type checkers
        raise RuntimeError("launch attempt projection returned no lifecycle")
    return RemoteLaunchResult(
        session_id=UUID(str(attempt.session_id)),
        launch_state=lifecycle.state,
        execution_lifetime=lifecycle.execution_lifetime,
        launch_error_code=lifecycle.error_code,
        launch_error_message=lifecycle.error_message,
    )


async def _write_live_launch_readiness(write_fn) -> None:
    if not database_module.live_store_configured():
        return
    live_ws = get_live_write_serializer()
    if not live_ws.is_configured:
        logger.warning("Live Store configured but live write serializer is unavailable for launch readiness")
        return
    try:
        await live_ws.execute(write_fn, label="launch-readiness")
    except Exception:
        logger.warning("Failed to write live launch readiness", exc_info=True)


async def _execute_live_launch_write(write_fn, *, label: str):
    if not database_module.live_store_configured():
        raise RemoteLaunchError(
            "Remote launch is blocked because Live Store is unavailable; retry shortly.",
            code="launch_timeout",
            status_code=503,
        )
    live_ws = get_live_write_serializer()
    if not live_ws.is_configured:
        raise RemoteLaunchError(
            "Remote launch is blocked because Live Store writer is unavailable; retry shortly.",
            code="launch_timeout",
            status_code=503,
        )
    try:
        return await live_ws.execute(write_fn, label=label)
    except RemoteLaunchError:
        raise
    except Exception as exc:
        logger.warning("Failed to write remote launch live fact", exc_info=True)
        raise RemoteLaunchError(
            "Remote launch is blocked because Live Store could not persist launch intent; retry shortly.",
            code="launch_timeout",
            status_code=503,
        ) from exc


async def _read_live_launch_idempotency(
    *,
    owner_id: int,
    device_id: str,
    provider: str,
    client_request_id: str,
):
    live_session_factory = database_module.get_live_session_factory()
    if live_session_factory is not None:
        with live_session_factory() as live_db:
            return get_live_launch_readiness_by_client_request(
                live_db,
                owner_id=owner_id,
                device_id=device_id,
                provider=provider,
                client_request_id=client_request_id,
            )
    return await _execute_live_launch_write(
        lambda live_db: get_live_launch_readiness_by_client_request(
            live_db,
            owner_id=owner_id,
            device_id=device_id,
            provider=provider,
            client_request_id=client_request_id,
        ),
        label="remote-launch-idempotency",
    )


def _control_plane_for_provider(provider: str | None) -> str:
    return control_plane_for_provider(provider)


def _attach_live_launch_run(
    db: Session,
    *,
    session: AgentSession,
    attempt: SessionLaunchAttempt,
    external_name: str | None,
    force_new_run: bool = False,
    provider_thread_id: str | None = None,
    thread_path: str | None = None,
    cwd: str | None = None,
) -> None:
    thread = ensure_primary_thread(db, session)
    now = datetime.now(timezone.utc)
    if provider_thread_id and not is_synthetic_provider_session_id(session, provider_thread_id):
        record_thread_alias(
            db,
            thread=thread,
            provider=session.provider,
            alias_kind="provider_session_id",
            alias_value=provider_thread_id,
        )
    if thread_path:
        record_thread_alias(
            db,
            thread=thread,
            provider=session.provider,
            alias_kind="source_path",
            alias_value=thread_path,
        )
    if force_new_run:
        _release_open_runs_for_thread(db, thread=thread, now=now)
    run = (
        record_run(
            db,
            thread=thread,
            provider=session.provider,
            host_id=session.device_id,
            cwd=cwd or session.cwd,
            launch_origin="longhouse_continued",
        )
        if force_new_run
        else ensure_open_run_for_session(
            db,
            session,
            launch_origin="longhouse_spawned",
            host_id=session.device_id,
        )
    )
    contract = require_contract_for_provider(session.provider)
    connection_capabilities = contract.connection_capabilities
    conn = upsert_connection_for_run(
        db,
        run=run,
        control_plane=contract.control_plane,
        acquisition_kind="spawned_control",
        state="attached",
        external_name=external_name or session.device_id,
        can_send_input=connection_capabilities["can_send_input"],
        can_interrupt=connection_capabilities["can_interrupt"],
        can_terminate=connection_capabilities["can_terminate"],
        can_tail_output=connection_capabilities["can_tail_output"],
        can_resume=connection_capabilities["can_resume"],
    )
    # The engine ack IS the readiness observation, so stamp health now. This
    # keeps the connection inside the lease freshness window the capability
    # projection enforces; the insert path of upsert leaves last_health_at NULL.
    conn.last_health_at = now
    if session.ended_at is not None:
        session.ended_at = None
    update_launch_attempt(
        db,
        attempt,
        state="adopted",
        run=run,
        clear_expires=True,
    )


def _attach_one_shot_launch_run(
    db: Session,
    *,
    session: AgentSession,
    attempt: SessionLaunchAttempt,
    external_name: str | None,
    pid: int | None,
    argv: list[str] | None,
    cwd: str,
) -> None:
    now = datetime.now(timezone.utc)
    run = db.get(SessionRun, attempt.run_id) if attempt.run_id is not None else None
    if run is None:
        thread = ensure_primary_thread(db, session)
        run = record_run(
            db,
            thread=thread,
            provider=session.provider,
            host_id=session.device_id,
            cwd=cwd or session.cwd,
            launch_origin="longhouse_spawned",
        )
    if pid is not None:
        run.pid = pid
    if argv:
        run.argv_redacted_json = argv
    run_ended_at = run.ended_at
    connection_state = "ended" if run_ended_at is not None else "attached"
    control_plane = ONE_SHOT_CONTROL_PLANE_BY_PROVIDER.get(session.provider, f"{session.provider}_exec")
    conn = upsert_connection_for_run(
        db,
        run=run,
        control_plane=control_plane,
        acquisition_kind="spawned_control",
        state=connection_state,
        external_name=external_name or session.device_id,
        device_id=session.device_id,
        can_send_input=0,
        can_interrupt=0,
        can_terminate=0,
        can_tail_output=0,
        can_resume=0,
    )
    if run_ended_at is not None:
        conn.released_at = run_ended_at
        conn.last_health_at = run_ended_at
    else:
        conn.last_health_at = now
    if session.ended_at is not None:
        session.ended_at = None
    update_launch_attempt(
        db,
        attempt,
        state="adopted",
        run=run,
        clear_expires=True,
    )


def _mark_one_shot_launch_run_failed(db: Session, *, attempt: SessionLaunchAttempt, error_code: str) -> None:
    if attempt.run_id is None:
        return
    run = db.get(SessionRun, attempt.run_id)
    if run is None:
        return
    now = datetime.now(timezone.utc)
    run_ended_at = run.ended_at
    if run_ended_at is None:
        run.ended_at = now
        run.exit_status = (error_code or "provider_launch_failed")[:64]
        connection_released_at = now
    else:
        connection_released_at = run_ended_at
        if not run.exit_status:
            run.exit_status = (error_code or "provider_launch_failed")[:64]
    for conn in (
        db.query(SessionConnection)
        .filter(SessionConnection.run_id == run.id)
        .filter(SessionConnection.state.in_(("attached", "degraded", "detached")))
        .all()
    ):
        conn.state = "ended"
        conn.released_at = connection_released_at
        conn.last_health_at = connection_released_at
        conn.can_send_input = 0
        conn.can_interrupt = 0
        conn.can_terminate = 0
        conn.can_tail_output = 0
        conn.can_resume = 0


def _release_open_runs_for_thread(db: Session, *, thread: SessionThread, now: datetime) -> None:
    run_query = db.query(SessionRun).filter(SessionRun.thread_id == thread.id)
    open_runs = run_query.filter(SessionRun.ended_at.is_(None)).all()
    if not open_runs:
        return

    open_run_ids = [run.id for run in open_runs]
    for run in open_runs:
        run.ended_at = now
    for conn in (
        db.query(SessionConnection)
        .filter(SessionConnection.run_id.in_(open_run_ids))
        .filter(SessionConnection.state.in_(("attached", "degraded")))
        .all()
    ):
        conn.state = "released"
        conn.released_at = now
        conn.last_health_at = now
        conn.can_send_input = 0
        conn.can_interrupt = 0
        conn.can_terminate = 0
        conn.can_tail_output = 0
        conn.can_resume = 0


def _result_resume_thread_id(message: Mapping | None) -> str | None:
    result = message.get("result") if isinstance(message, Mapping) else None
    if not isinstance(result, dict):
        return None
    value = result.get("provider_session_id") or result.get("thread_id")
    return str(value).strip() if value else None


def _result_resume_thread_path(message: Mapping | None) -> str | None:
    result = message.get("result") if isinstance(message, Mapping) else None
    if not isinstance(result, dict):
        return None
    value = result.get("thread_path")
    return str(value).strip() if value else None


def _result_pid(message: Mapping | None) -> int | None:
    result = message.get("result") if isinstance(message, Mapping) else None
    if not isinstance(result, dict):
        return None
    value = result.get("pid")
    if value is None or isinstance(value, bool):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _result_argv(message: Mapping | None) -> list[str] | None:
    result = message.get("result") if isinstance(message, Mapping) else None
    if not isinstance(result, dict):
        return None
    value = result.get("argv")
    if not isinstance(value, list):
        return None
    argv = [str(item) for item in value if str(item).strip()]
    return argv or None


def _resolve_continue_target(db: Session, *, session: AgentSession) -> tuple[SessionThread, str, str | None]:
    """Resolve the provider resume target for a continuable session.

    Returns ``(thread, provider_resume_id, thread_path)``. ``provider_resume_id``
    is the id passed to the provider's resume flag (the real provider identity
    from the provider_session_id alias, not the longhouse id). ``thread_path`` is
    the transcript path for file-resuming providers (codex) and ``None`` for
    providers that resume by id alone (claude).

    Delegates the managed/unmanaged + id decision to the shared resolver so the
    Continue button (view) and this execution path can never disagree.
    """

    provider = (session.provider or "").strip().lower()
    if provider not in continue_supported_providers():
        raise RemoteLaunchError(
            f"provider {session.provider!r} is not supported for session continuation in v1",
            code="provider_unsupported",
            status_code=400,
        )

    resolution = resolve_native_continue_target(db, session)
    if resolution is None:
        # No resolvable resume identity. Give a provider-shaped reason.
        if provider == "claude":
            raise RemoteLaunchError(
                "Session has no resolvable Claude resume identity; cannot continue",
                code="invalid_request",
                status_code=409,
            )
        raise RemoteLaunchError(
            "Session is missing a Codex thread id or transcript path; cannot continue",
            code="invalid_request",
            status_code=409,
        )
    return resolution.thread, resolution.provider_resume_id, resolution.source_path


def _remote_launch_result_from_live_view(view) -> RemoteLaunchResult:
    return RemoteLaunchResult(
        session_id=view.session_id,
        launch_state=view.launch_state,
        execution_lifetime=view.execution_lifetime,
        launch_error_code=view.launch_error_code,
        launch_error_message=view.launch_error_message,
    )


def _build_remote_launch_live_payload(
    *,
    session_uuid: UUID,
    run_uuid: UUID | None,
    params: RemoteLaunchParams,
    provider: str,
    device_id: str,
    cwd: str,
    execution_lifetime: RemoteExecutionLifetime,
    command_id: str,
    project: str,
    display_name: str,
    machine_id: str | None,
    started_at: datetime,
    expires_at: datetime,
) -> dict:
    payload = {
        "session_id": str(session_uuid),
        "owner_id": int(params.owner_id),
        "device_id": device_id,
        "machine_id": machine_id or device_id,
        "provider": provider,
        "cwd": cwd,
        "git_repo": params.git_repo,
        "git_branch": params.git_branch,
        "project": project,
        "display_name": display_name,
        "initial_prompt": params.initial_prompt,
        "execution_lifetime": execution_lifetime,
        "client_request_id": (params.client_request_id or "").strip() or None,
        "command_id": command_id,
        "started_at": started_at,
        "expires_at": expires_at,
    }
    if run_uuid is not None:
        payload["run_id"] = str(run_uuid)
    return payload


async def _launch_remote_session_hot(
    db: Session,
    params: RemoteLaunchParams,
    *,
    registry: MachineControlChannelRegistry,
    provider: str,
    execution_lifetime: RemoteExecutionLifetime,
    device_id: str,
    cwd: str,
    initial_prompt: str,
    client_request_id: str | None,
    machine_name: str | None,
) -> RemoteLaunchResult:
    if client_request_id:
        existing = await _read_live_launch_idempotency(
            owner_id=params.owner_id,
            device_id=device_id,
            provider=provider,
            client_request_id=client_request_id,
        )
        if existing is not None:
            return _remote_launch_result_from_live_view(existing)

    session_uuid = uuid4()
    run_uuid = uuid4() if execution_lifetime == "one_shot" else None
    command_id = f"launch-{session_uuid}"
    project = _project_for(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=LAUNCH_LEASE_SECS)
    launch_payload = _build_remote_launch_live_payload(
        session_uuid=session_uuid,
        run_uuid=run_uuid,
        params=params,
        provider=provider,
        device_id=device_id,
        cwd=cwd,
        execution_lifetime=execution_lifetime,
        command_id=command_id,
        project=project,
        display_name=display_name,
        machine_id=machine_name,
        started_at=now,
        expires_at=lease_until,
    )

    await _execute_live_launch_write(
        lambda live_db: (
            upsert_live_launch_readiness(
                live_db,
                session_id=session_uuid,
                owner_id=params.owner_id,
                device_id=device_id,
                provider=provider,
                execution_lifetime=execution_lifetime,
                state="pending",
                command_id=command_id,
                client_request_id=client_request_id,
                machine_id=machine_name or device_id,
                project=project,
                expires_at=lease_until,
                now=now,
            ),
            enqueue_remote_launch_outbox(live_db, launch=launch_payload),
        ),
        label="remote-launch-intent",
    )

    payload = {
        "provider": provider,
        "cwd": cwd,
        "execution_lifetime": execution_lifetime,
        "git_repo": params.git_repo,
        "git_branch": params.git_branch,
        "project": project,
        "display_name": display_name,
    }
    if execution_lifetime == "one_shot":
        payload["initial_prompt"] = initial_prompt
        payload["run_id"] = str(run_uuid)
    response: MachineControlCommandResponse = await registry.send_command(
        owner_id=params.owner_id,
        device_id=device_id,
        session_id=str(session_uuid),
        command_type="session.run_once" if execution_lifetime == "one_shot" else "session.launch",
        payload=payload,
        timeout_secs=LAUNCH_COMMAND_TIMEOUT_SECS,
        command_id=command_id,
    )

    if not response.transport_ok:
        error_message = response.error or "control channel transport failed"
        outcome = {"state": "dispatched", "error_message": error_message}
        await _write_live_launch_readiness(
            lambda live_db: (
                update_live_launch_readiness_state(
                    live_db,
                    session_id=session_uuid,
                    state="dispatched",
                    error_message=error_message,
                ),
                enqueue_remote_launch_outcome_outbox(live_db, launch=launch_payload, outcome=outcome),
            )
        )
        return RemoteLaunchResult(
            session_id=session_uuid,
            launch_state="launching_unknown",
            execution_lifetime=execution_lifetime,
        )

    message = response.message or {}
    if message.get("ok"):
        outcome = {
            "state": "adopted",
            "pid": _result_pid(message),
            "argv": _result_argv(message),
            "provider_thread_id": _result_resume_thread_id(message),
            "thread_path": _result_resume_thread_path(message),
            "external_name": machine_name or device_id,
        }
        await _write_live_launch_readiness(
            lambda live_db: (
                update_live_launch_readiness_state(
                    live_db,
                    session_id=session_uuid,
                    state="adopted",
                    clear_expires=True,
                ),
                enqueue_remote_launch_outcome_outbox(live_db, launch=launch_payload, outcome=outcome),
            )
        )
        elapsed_ms = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)
        logger.info(
            "remote_launch session=%s device=%s provider=%s lifetime=%s state=live duration_ms=%s",
            session_uuid,
            device_id,
            provider,
            execution_lifetime,
            elapsed_ms,
        )
        return RemoteLaunchResult(
            session_id=session_uuid,
            launch_state="live",
            execution_lifetime=execution_lifetime,
        )

    error = message.get("error") or {}
    code = normalize_remote_launch_error_code(error.get("code"))
    err_msg = str(error.get("message") or "unknown error")
    outcome = {"state": "failed", "error_code": code, "error_message": err_msg}
    await _write_live_launch_readiness(
        lambda live_db: (
            update_live_launch_readiness_state(
                live_db,
                session_id=session_uuid,
                state="failed",
                error_code=code,
                error_message=err_msg,
                clear_expires=True,
            ),
            enqueue_remote_launch_outcome_outbox(live_db, launch=launch_payload, outcome=outcome),
        )
    )
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
        execution_lifetime=execution_lifetime,
        launch_error_code=code,
        launch_error_message=err_msg,
    )


def _remote_continue_outbox_key(*, session_id: UUID, client_request_id: str) -> str:
    return f"{REMOTE_LAUNCH_KIND}:continue:{session_id}:{client_request_id}"


def _remote_continue_outcome_outbox_key(*, session_id: UUID, client_request_id: str, state: str) -> str:
    return f"{REMOTE_LAUNCH_OUTCOME_KIND}:continue:{session_id}:{client_request_id}:{state}"


async def _continue_remote_session_hot(
    *,
    session: AgentSession,
    params: RemoteContinueParams,
    registry: MachineControlChannelRegistry,
    info,
    device_id: str,
    cwd: str,
    provider: str,
    execution_lifetime: RemoteExecutionLifetime,
    message: str,
    client_request_id: str,
    provider_thread_id: str,
    thread_path: str | None,
) -> RemoteLaunchResult:
    existing = await _read_live_launch_idempotency(
        owner_id=params.owner_id,
        device_id=device_id,
        provider=provider,
        client_request_id=client_request_id,
    )
    if existing is not None:
        return _remote_launch_result_from_live_view(existing)

    session_uuid = UUID(str(session.id))
    run_uuid = uuid4()
    command_id = f"continue-{uuid4()}"
    project = (session.project or _project_for(cwd, None)).strip() or "managed-local"
    display_name = project
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=LAUNCH_LEASE_SECS)
    launch_payload = {
        "session_id": str(session_uuid),
        "owner_id": int(params.owner_id),
        "device_id": device_id,
        "machine_id": info.machine_name or device_id,
        "provider": provider,
        "cwd": cwd,
        "git_repo": session.git_repo,
        "git_branch": session.git_branch,
        "project": project,
        "display_name": display_name,
        "initial_prompt": message if execution_lifetime == "one_shot" else None,
        "execution_lifetime": execution_lifetime,
        "client_request_id": client_request_id,
        "command_id": command_id,
        "started_at": now,
        "expires_at": lease_until,
        "mode": "continue",
        "launch_origin": "longhouse_continued",
        "resume": {
            "thread_id": provider_thread_id,
            "thread_path": thread_path,
        },
    }
    launch_payload["run_id"] = str(run_uuid)

    await _execute_live_launch_write(
        lambda live_db: (
            upsert_live_launch_readiness(
                live_db,
                session_id=session_uuid,
                owner_id=params.owner_id,
                device_id=device_id,
                provider=provider,
                execution_lifetime=execution_lifetime,
                state="pending",
                command_id=command_id,
                client_request_id=client_request_id,
                machine_id=info.machine_name or device_id,
                project=project,
                expires_at=lease_until,
                now=now,
            ),
            enqueue_remote_launch_outbox(
                live_db,
                launch=launch_payload,
                idempotency_key=_remote_continue_outbox_key(
                    session_id=session_uuid,
                    client_request_id=client_request_id,
                ),
            ),
        ),
        label="remote-continue-intent",
    )

    payload = {
        "provider": provider,
        "cwd": cwd,
        "git_repo": session.git_repo,
        "git_branch": session.git_branch,
        "project": project,
        "display_name": display_name,
        "mode": "continue",
        "resume": {
            "thread_id": provider_thread_id,
            "thread_path": thread_path,
        },
    }
    if execution_lifetime == "one_shot":
        payload["execution_lifetime"] = execution_lifetime
        payload["initial_prompt"] = message
        if run_uuid is not None:
            payload["run_id"] = str(run_uuid)

    response: MachineControlCommandResponse = await registry.send_command(
        owner_id=params.owner_id,
        device_id=device_id,
        session_id=str(session_uuid),
        command_type="session.run_once" if execution_lifetime == "one_shot" else "session.launch",
        payload=payload,
        timeout_secs=LAUNCH_COMMAND_TIMEOUT_SECS,
        command_id=command_id,
    )

    if not response.transport_ok:
        error_message = response.error or "control channel transport failed"
        outcome = {"state": "dispatched", "error_message": error_message}
        await _write_live_launch_readiness(
            lambda live_db: (
                update_live_launch_readiness_state(
                    live_db,
                    session_id=session_uuid,
                    state="dispatched",
                    error_message=error_message,
                ),
                enqueue_remote_launch_outcome_outbox(
                    live_db,
                    launch=launch_payload,
                    outcome=outcome,
                    idempotency_key=_remote_continue_outcome_outbox_key(
                        session_id=session_uuid,
                        client_request_id=client_request_id,
                        state="dispatched",
                    ),
                ),
            )
        )
        return RemoteLaunchResult(
            session_id=session_uuid,
            launch_state="launching_unknown",
            execution_lifetime=execution_lifetime,
        )

    response_message = response.message or {}
    if response_message.get("ok"):
        outcome = {
            "state": "adopted",
            "pid": _result_pid(response_message),
            "argv": _result_argv(response_message),
            "provider_thread_id": _result_resume_thread_id(response_message) or provider_thread_id,
            "thread_path": _result_resume_thread_path(response_message) or thread_path,
            "external_name": info.machine_name or device_id,
        }
        await _write_live_launch_readiness(
            lambda live_db: (
                update_live_launch_readiness_state(
                    live_db,
                    session_id=session_uuid,
                    state="adopted",
                    clear_expires=True,
                ),
                enqueue_remote_launch_outcome_outbox(
                    live_db,
                    launch=launch_payload,
                    outcome=outcome,
                    idempotency_key=_remote_continue_outcome_outbox_key(
                        session_id=session_uuid,
                        client_request_id=client_request_id,
                        state="adopted",
                    ),
                ),
            )
        )
        elapsed_ms = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)
        logger.info(
            "remote_continue session=%s device=%s provider=%s state=live duration_ms=%s",
            session_uuid,
            device_id,
            provider,
            elapsed_ms,
        )
        return RemoteLaunchResult(
            session_id=session_uuid,
            launch_state="live",
            execution_lifetime=execution_lifetime,
        )

    error = response_message.get("error") or {}
    code = normalize_remote_launch_error_code(error.get("code"))
    err_msg = str(error.get("message") or "unknown error")
    outcome = {"state": "failed", "error_code": code, "error_message": err_msg}
    await _write_live_launch_readiness(
        lambda live_db: (
            update_live_launch_readiness_state(
                live_db,
                session_id=session_uuid,
                state="failed",
                error_code=code,
                error_message=err_msg,
                clear_expires=True,
            ),
            enqueue_remote_launch_outcome_outbox(
                live_db,
                launch=launch_payload,
                outcome=outcome,
                idempotency_key=_remote_continue_outcome_outbox_key(
                    session_id=session_uuid,
                    client_request_id=client_request_id,
                    state="failed",
                ),
            ),
        )
    )
    logger.warning(
        "remote_continue session=%s device=%s provider=%s state=launch_failed code=%s",
        session_uuid,
        device_id,
        provider,
        code,
    )
    return RemoteLaunchResult(
        session_id=session_uuid,
        launch_state="launch_failed",
        execution_lifetime=execution_lifetime,
        launch_error_code=code,
        launch_error_message=err_msg,
    )


async def launch_remote_session(
    db: Session,
    params: RemoteLaunchParams,
    *,
    registry: MachineControlChannelRegistry | None = None,
) -> RemoteLaunchResult:
    provider = (params.provider or "").strip().lower()
    execution_lifetime = normalize_remote_execution_lifetime(params.execution_lifetime)

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
    initial_prompt = (params.initial_prompt or "").strip()
    if execution_lifetime == "one_shot" and not initial_prompt:
        raise RemoteLaunchError(
            "initial_prompt is required for one-shot remote launch",
            code="invalid_request",
            status_code=400,
        )
    supported_providers = RUN_ONCE_SUPPORTED_PROVIDERS if execution_lifetime == "one_shot" else LIVE_CONTROL_SUPPORTED_PROVIDERS
    if provider not in supported_providers:
        raise RemoteLaunchError(
            f"provider {provider!r} is not supported for {execution_lifetime} remote launch",
            code="provider_unsupported",
            status_code=400,
        )

    _verify_device_owned_by(db, owner_id=params.owner_id, device_id=device_id)

    client_request_id = (params.client_request_id or "").strip() or None
    if client_request_id and not database_module.live_store_configured():
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
    launch_cap = f"{provider}.run_once" if execution_lifetime == "one_shot" else f"{provider}.launch"
    if launch_cap not in info.supports:
        raise RemoteLaunchError(
            f"Machine {device_id!r} does not support {launch_cap}",
            code="provider_unsupported",
            status_code=409,
        )

    if database_module.live_store_configured():
        return await _launch_remote_session_hot(
            db,
            params,
            registry=reg,
            provider=provider,
            execution_lifetime=execution_lifetime,
            device_id=device_id,
            cwd=cwd,
            initial_prompt=initial_prompt,
            client_request_id=client_request_id,
            machine_name=info.machine_name or device_id,
        )

    session_uuid = uuid4()
    command_id = f"launch-{session_uuid}"
    project = _project_for(cwd, params.project)
    display_name = (params.display_name or project).strip() or project
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=LAUNCH_LEASE_SECS)

    await _write_live_launch_readiness(
        lambda live_db: upsert_live_launch_readiness(
            live_db,
            session_id=session_uuid,
            owner_id=params.owner_id,
            device_id=device_id,
            provider=provider,
            execution_lifetime=execution_lifetime,
            state="pending",
            command_id=command_id,
            client_request_id=client_request_id,
            machine_id=info.machine_name or device_id,
            project=project,
            expires_at=lease_until,
            now=now,
        )
    )

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
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        loop_mode=SessionLoopMode.ASSIST.value,
    )
    db.add(session)
    db.flush()

    # Phase 2 dual-write: materialize kernel rows alongside legacy launch_*.
    primary_thread = ensure_primary_thread(db, session)
    launch_attempt = record_launch_attempt(
        db,
        session=session,
        thread=primary_thread,
        provider=provider,
        host_id=device_id,
        owner_id=params.owner_id,
        execution_lifetime=execution_lifetime,
        client_request_id=client_request_id,
        command_id=command_id,
        state="pending",
        expires_at=lease_until,
    )
    one_shot_run: SessionRun | None = None
    if execution_lifetime == "one_shot":
        one_shot_run = record_run(
            db,
            thread=primary_thread,
            provider=provider,
            host_id=device_id,
            cwd=cwd,
            launch_origin="longhouse_spawned",
        )
        launch_attempt.run_id = one_shot_run.id
    db.commit()
    db.refresh(session)

    payload = {
        "provider": provider,
        "cwd": cwd,
        "execution_lifetime": execution_lifetime,
        "git_repo": params.git_repo,
        "git_branch": params.git_branch,
        "project": project,
        "display_name": display_name,
    }
    if execution_lifetime == "one_shot":
        payload["initial_prompt"] = initial_prompt
        if one_shot_run is not None:
            payload["run_id"] = str(one_shot_run.id)
    response: MachineControlCommandResponse = await reg.send_command(
        owner_id=params.owner_id,
        device_id=device_id,
        session_id=str(session_uuid),
        command_type="session.run_once" if execution_lifetime == "one_shot" else "session.launch",
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
        await _write_live_launch_readiness(
            lambda live_db: update_live_launch_readiness_state(
                live_db,
                session_id=session_uuid,
                state="dispatched",
                error_message=error_message,
            )
        )
        db.commit()
        db.refresh(session)
        return _launch_result_for_attempt(launch_attempt)

    message = response.message or {}
    if message.get("ok"):
        if execution_lifetime == "one_shot":
            _attach_one_shot_launch_run(
                db,
                session=session,
                attempt=launch_attempt,
                external_name=info.machine_name or device_id,
                pid=_result_pid(message),
                argv=_result_argv(message),
                cwd=cwd,
            )
        else:
            _attach_live_launch_run(
                db,
                session=session,
                attempt=launch_attempt,
                external_name=info.machine_name or device_id,
                provider_thread_id=_result_resume_thread_id(message),
                thread_path=_result_resume_thread_path(message),
                cwd=cwd,
            )
        db.commit()
        await _write_live_launch_readiness(
            lambda live_db: update_live_launch_readiness_state(
                live_db,
                session_id=session_uuid,
                state="adopted",
                clear_expires=True,
            )
        )
        db.refresh(session)
        elapsed_ms = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)
        logger.info(
            "remote_launch session=%s device=%s provider=%s lifetime=%s state=live duration_ms=%s",
            session_uuid,
            device_id,
            provider,
            execution_lifetime,
            elapsed_ms,
        )
        return _launch_result_for_attempt(launch_attempt)

    error = message.get("error") or {}
    code = normalize_remote_launch_error_code(error.get("code"))
    err_msg = str(error.get("message") or "unknown error")
    session.ended_at = datetime.now(timezone.utc)
    if execution_lifetime == "one_shot":
        _mark_one_shot_launch_run_failed(db, attempt=launch_attempt, error_code=code)
    update_launch_attempt(
        db,
        launch_attempt,
        state="failed",
        error_code=code,
        error_message=err_msg,
        clear_expires=True,
    )
    await _write_live_launch_readiness(
        lambda live_db: update_live_launch_readiness_state(
            live_db,
            session_id=session_uuid,
            state="failed",
            error_code=code,
            error_message=err_msg,
            clear_expires=True,
        )
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
    return _launch_result_for_attempt(launch_attempt)


async def continue_remote_session(
    db: Session,
    params: RemoteContinueParams,
    *,
    registry: MachineControlChannelRegistry | None = None,
) -> RemoteLaunchResult:
    """Start a new managed Codex process on an existing Longhouse session/thread."""

    session = db.query(AgentSession).filter(AgentSession.id == params.session_id).first()
    if session is None:
        raise RemoteLaunchError(
            f"Session {params.session_id} was not found",
            code="invalid_request",
            status_code=404,
        )

    device_id = (params.device_id or session.device_id or "").strip()
    if not device_id:
        raise RemoteLaunchError(
            "device_id is required because the session has no recorded host",
            code="invalid_request",
            status_code=400,
        )
    cwd = (params.cwd or session.cwd or "").strip()
    if not cwd:
        raise RemoteLaunchError(
            "cwd is required because the session has no recorded working directory",
            code="invalid_request",
            status_code=400,
        )
    if not cwd.startswith("/"):
        raise RemoteLaunchError(
            "cwd must be absolute",
            code="cwd_not_allowed",
            status_code=400,
        )
    execution_lifetime = normalize_remote_execution_lifetime(params.execution_lifetime)
    message = (params.message or "").strip()
    if execution_lifetime == "one_shot" and not message:
        raise RemoteLaunchError(
            "message is required for one-shot session continuation",
            code="invalid_request",
            status_code=400,
        )

    provider = (session.provider or "").strip().lower()
    if provider not in continue_supported_providers():
        raise RemoteLaunchError(
            f"provider {provider!r} is not supported for session continuation in v1",
            code="provider_unsupported",
            status_code=400,
        )
    if execution_lifetime == "one_shot" and provider not in RUN_ONCE_SUPPORTED_PROVIDERS:
        raise RemoteLaunchError(
            f"provider {provider!r} is not supported for one-shot session continuation",
            code="provider_unsupported",
            status_code=400,
        )

    _verify_device_owned_by(db, owner_id=params.owner_id, device_id=device_id)
    source_device_id = (session.device_id or "").strip()
    if not source_device_id:
        raise RemoteLaunchError(
            "Session cannot be continued because it has no recorded source host",
            code="invalid_request",
            status_code=409,
        )
    _verify_device_owned_by(db, owner_id=params.owner_id, device_id=source_device_id)

    client_request_id = (params.client_request_id or "").strip() or None
    if not client_request_id:
        raise RemoteLaunchError(
            "client_request_id is required for session continuation",
            code="invalid_request",
            status_code=400,
        )

    caps = project_session_capabilities(db, session_id=session.id)
    if caps.live_control_available and caps.can_send_input:
        if execution_lifetime == "live_control":
            return RemoteLaunchResult(
                session_id=UUID(str(session.id)),
                launch_state="live",
                execution_lifetime=DEFAULT_REMOTE_EXECUTION_LIFETIME,
            )
        raise RemoteLaunchError(
            "Session is already live; send input to the live session instead",
            code="invalid_request",
            status_code=409,
        )

    thread, provider_thread_id, thread_path = _resolve_continue_target(db, session=session)

    # Persist the resolved transcript path as a thread alias eagerly, so the
    # resume target survives even if the synchronous launch times out and the
    # late command-result reconciliation never arrives. (The resolver itself is
    # read-only; recording belongs here at the explicit write boundary.)
    if thread_path:
        record_thread_alias(
            db,
            thread=thread,
            provider=provider,
            alias_kind="source_path",
            alias_value=thread_path,
        )

    existing = (
        db.query(SessionLaunchAttempt)
        .filter(SessionLaunchAttempt.session_id == session.id)
        .filter(SessionLaunchAttempt.client_request_id == client_request_id)
        .filter(SessionLaunchAttempt.owner_id == params.owner_id)
        .filter(SessionLaunchAttempt.host_id == device_id)
        .filter(SessionLaunchAttempt.provider == provider)
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
    continue_cap = f"{provider}.resume_run_once" if execution_lifetime == "one_shot" else f"{provider}.continue"
    if continue_cap not in info.supports:
        raise RemoteLaunchError(
            f"Machine {device_id!r} does not support {continue_cap}",
            code="provider_unsupported",
            status_code=409,
        )

    if database_module.live_store_configured():
        return await _continue_remote_session_hot(
            session=session,
            params=params,
            registry=reg,
            info=info,
            device_id=device_id,
            cwd=cwd,
            provider=provider,
            execution_lifetime=execution_lifetime,
            message=message,
            client_request_id=client_request_id,
            provider_thread_id=provider_thread_id,
            thread_path=thread_path,
        )

    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=LAUNCH_LEASE_SECS)
    command_id = f"continue-{uuid4()}"
    launch_attempt = record_launch_attempt(
        db,
        session=session,
        thread=thread,
        provider=provider,
        host_id=device_id,
        owner_id=params.owner_id,
        execution_lifetime=execution_lifetime,
        client_request_id=client_request_id,
        command_id=command_id,
        state="pending",
        expires_at=lease_until,
    )
    one_shot_run: SessionRun | None = None
    if execution_lifetime == "one_shot":
        _release_open_runs_for_thread(db, thread=thread, now=now)
        one_shot_run = record_run(
            db,
            thread=thread,
            provider=provider,
            host_id=device_id,
            cwd=cwd,
            launch_origin="longhouse_continued",
        )
        launch_attempt.run_id = one_shot_run.id
    session.device_id = device_id
    session.device_name = info.machine_name or device_id
    session.cwd = cwd
    db.commit()
    db.refresh(session)

    payload = {
        "provider": provider,
        "cwd": cwd,
        "git_repo": session.git_repo,
        "git_branch": session.git_branch,
        "project": session.project,
        "display_name": session.project,
        "mode": "continue",
        "resume": {
            "thread_id": provider_thread_id,
            "thread_path": thread_path,
        },
    }
    if execution_lifetime == "one_shot":
        payload["execution_lifetime"] = execution_lifetime
        payload["initial_prompt"] = message
        if one_shot_run is not None:
            payload["run_id"] = str(one_shot_run.id)
    response: MachineControlCommandResponse = await reg.send_command(
        owner_id=params.owner_id,
        device_id=device_id,
        session_id=str(session.id),
        command_type="session.run_once" if execution_lifetime == "one_shot" else "session.launch",
        payload=payload,
        timeout_secs=LAUNCH_COMMAND_TIMEOUT_SECS,
        command_id=command_id,
    )

    if not response.transport_ok:
        update_launch_attempt(
            db,
            launch_attempt,
            state="dispatched",
            error_message=response.error or "control channel transport failed",
        )
        db.commit()
        db.refresh(session)
        return _launch_result_for_attempt(launch_attempt)

    response_message = response.message or {}
    if response_message.get("ok"):
        if execution_lifetime == "one_shot":
            _attach_one_shot_launch_run(
                db,
                session=session,
                attempt=launch_attempt,
                external_name=info.machine_name or device_id,
                pid=_result_pid(response_message),
                argv=_result_argv(response_message),
                cwd=cwd,
            )
        else:
            _attach_live_launch_run(
                db,
                session=session,
                attempt=launch_attempt,
                external_name=info.machine_name or device_id,
                force_new_run=True,
                provider_thread_id=_result_resume_thread_id(response_message) or provider_thread_id,
                thread_path=_result_resume_thread_path(response_message) or thread_path,
                cwd=cwd,
            )
        db.commit()
        db.refresh(session)
        elapsed_ms = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)
        logger.info(
            "remote_continue session=%s device=%s provider=%s state=live duration_ms=%s",
            session.id,
            device_id,
            provider,
            elapsed_ms,
        )
        return _launch_result_for_attempt(launch_attempt)

    error = response_message.get("error") or {}
    code = normalize_remote_launch_error_code(error.get("code"))
    err_msg = str(error.get("message") or "unknown error")
    if execution_lifetime == "one_shot":
        _mark_one_shot_launch_run_failed(db, attempt=launch_attempt, error_code=code)
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
        "remote_continue session=%s device=%s provider=%s state=launch_failed code=%s",
        session.id,
        device_id,
        provider,
        code,
    )
    return _launch_result_for_attempt(launch_attempt)


def _reconcile_live_launch_from_command_result(message: dict, *, command_id: str) -> bool:
    if not command_id.startswith("launch-") or not database_module.live_store_configured():
        return False
    session_id_text = command_id.removeprefix("launch-")
    try:
        session_uuid = UUID(session_id_text)
    except ValueError:
        return False
    reported_session_id = str(message.get("session_id") or "").strip()
    if reported_session_id and reported_session_id != str(session_uuid):
        return False

    live_session_factory = database_module.get_live_write_session_factory()
    if live_session_factory is None:
        return False

    def _write(live_db: Session) -> bool:
        outbox = (
            live_db.query(LiveArchiveOutbox)
            .filter(LiveArchiveOutbox.kind == REMOTE_LAUNCH_KIND)
            .filter(LiveArchiveOutbox.idempotency_key == remote_launch_idempotency_key(session_id=session_uuid))
            .order_by(LiveArchiveOutbox.id.desc())
            .first()
        )
        if outbox is None:
            return False
        payload = json.loads(outbox.payload_json or "{}")
        launch = payload.get("launch") or {}
        if message.get("ok"):
            outcome = {
                "state": "adopted",
                "pid": _result_pid(message),
                "argv": _result_argv(message),
                "provider_thread_id": _result_resume_thread_id(message),
                "thread_path": _result_resume_thread_path(message),
                "external_name": launch.get("machine_id") or launch.get("device_id"),
            }
            update_live_launch_readiness_state(
                live_db,
                session_id=session_uuid,
                state="adopted",
                clear_expires=True,
            )
        else:
            error = message.get("error") or {}
            code = normalize_remote_launch_error_code(error.get("code"))
            outcome = {
                "state": "failed",
                "error_code": code,
                "error_message": str(error.get("message") or "unknown error"),
            }
            update_live_launch_readiness_state(
                live_db,
                session_id=session_uuid,
                state="failed",
                error_code=code,
                error_message=str(error.get("message") or "unknown error"),
                clear_expires=True,
            )
        enqueue_remote_launch_outcome_outbox(live_db, launch=launch, outcome=outcome)
        return True

    try:
        with live_session_factory() as live_db:
            result = _write(live_db)
            live_db.commit()
            return bool(result)
    except Exception:
        logger.warning("Failed to reconcile late remote launch result through Live Store", exc_info=True)
        return False


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
    if not command_id or not (command_id.startswith("launch-") or command_id.startswith("continue-")):
        return False
    attempt = db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.command_id == command_id).first()
    if attempt is None:
        return _reconcile_live_launch_from_command_result(message, command_id=command_id)
    execution_lifetime = normalize_remote_execution_lifetime(attempt.execution_lifetime)
    if attempt.state == "adopted":
        return True
    if execution_lifetime != "one_shot" and attempt.run_id is not None:
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
        if execution_lifetime == "one_shot":
            _attach_one_shot_launch_run(
                db,
                session=session,
                attempt=attempt,
                external_name=session.device_name or session.device_id,
                pid=_result_pid(message),
                argv=_result_argv(message),
                cwd=session.cwd,
            )
        else:
            _attach_live_launch_run(
                db,
                session=session,
                attempt=attempt,
                external_name=session.device_name or session.device_id,
                force_new_run=command_id.startswith("continue-"),
                provider_thread_id=_result_resume_thread_id(message),
                thread_path=_result_resume_thread_path(message),
                cwd=session.cwd,
            )
    else:
        error = message.get("error") or {}
        if command_id.startswith("launch-") and session.ended_at is None:
            session.ended_at = datetime.now(timezone.utc)
        if execution_lifetime == "one_shot":
            _mark_one_shot_launch_run_failed(
                db,
                attempt=attempt,
                error_code=normalize_remote_launch_error_code(error.get("code")),
            )
        update_launch_attempt(
            db,
            attempt,
            state="failed",
            error_code=normalize_remote_launch_error_code(error.get("code")),
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
        command_id = str(attempt.command_id or "")
        if session is not None and session.ended_at is None and not command_id.startswith("continue-"):
            session.ended_at = cutoff
        if normalize_remote_execution_lifetime(attempt.execution_lifetime) == "one_shot":
            _mark_one_shot_launch_run_failed(db, attempt=attempt, error_code="launch_timeout")
        update_launch_attempt(
            db,
            attempt,
            state="abandoned",
            error_code=normalize_remote_launch_error_code(attempt.error_code, fallback="launch_timeout"),
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
    "RemoteContinueParams",
    "RemoteLaunchParams",
    "RemoteLaunchResult",
    "continue_remote_session",
    "launch_remote_session",
    "reap_orphaned_launches",
    "reconcile_launch_from_command_result",
]
