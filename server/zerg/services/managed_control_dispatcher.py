"""Managed-session control dispatch transports.

This seam is intentionally small during the migration off Runner-backed
control. Provider-specific command construction still lives in
``managed_local_control``; this module only chooses and invokes the control
delivery transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Mapping

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_provider_contracts import machine_control_capability_for_command
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.session_execution_home import SessionExecutionHome

MANAGED_CONTROL_COMMAND_INTERRUPT = "session.interrupt"
MANAGED_CONTROL_COMMAND_SEND_TEXT = "session.send_text"
MANAGED_CONTROL_COMMAND_STEER_TEXT = "session.steer_text"
MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL = "engine_channel"
MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER = "legacy_runner"
MANAGED_CONTROL_TRANSPORT_NONE = "none"
MISSING_LEGACY_RUNNER_METADATA_ERROR = "Managed local session is missing source runner metadata"


@dataclass(frozen=True)
class ManagedControlDispatchResult:
    ok: bool
    transport: str
    data: Mapping[str, Any] | None = None
    error: str | None = None


def _session_device_id(session: AgentSession | None) -> str | None:
    device_id = str(getattr(session, "device_id", "") or "").strip()
    return device_id or None


def _session_uses_engine_control(
    session: AgentSession,
    *,
    owner_id: int | None,
    command_type: str | None,
) -> bool:
    if owner_id is None or command_type is None:
        return False
    capability = machine_control_capability_for_command(getattr(session, "provider", None), command_type)
    device_id = _session_device_id(session)
    if capability is None or device_id is None:
        return False
    return get_machine_control_channel_registry().supports(
        owner_id=owner_id,
        device_id=device_id,
        capability=capability,
    )


def select_managed_control_transport(
    session: AgentSession | None,
    *,
    owner_id: int | None = None,
    command_type: str | None = None,
) -> str | None:
    """Return the explicit control transport for a managed session.

    Phase 1 preserves existing behavior: sessions with legacy Runner metadata
    use Runner-backed dispatch; sessions without it have no remote control
    transport until the Machine Agent channel lands.
    """

    if session is None:
        return None
    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return None
    if _session_uses_engine_control(session, owner_id=owner_id, command_type=command_type):
        return MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
    if getattr(session, "source_runner_id", None) is not None:
        return MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER
    return None


def _runner_dispatch_error(result: Mapping[str, Any], fallback: str) -> str:
    error = result.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or fallback)
    if error:
        return str(error)
    return fallback


def _engine_command_id(
    *,
    session: AgentSession,
    command_type: str | None,
    commis_id: str | None,
    run_id: str | None,
) -> str | None:
    seed = str(commis_id or run_id or "").strip()
    command = str(command_type or "").strip()
    if not seed or not command:
        return None
    return f"managed-control:{getattr(session, 'id')}:{command}:{seed}"


async def dispatch_managed_control_command(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    command: str,
    timeout_secs: int,
    command_type: str | None = None,
    payload: Mapping[str, Any] | None = None,
    commis_id: str | None = None,
    run_id: str | None = None,
    failure_message: str = "Failed to dispatch managed control command",
) -> ManagedControlDispatchResult:
    """Dispatch one managed-control command through the selected transport."""

    transport = select_managed_control_transport(session, owner_id=owner_id, command_type=command_type)
    if transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL:
        return await _dispatch_engine_channel(
            owner_id=owner_id,
            session=session,
            command_type=command_type,
            payload=payload,
            timeout_secs=timeout_secs,
            command_id=_engine_command_id(
                session=session,
                command_type=command_type,
                commis_id=commis_id,
                run_id=run_id,
            ),
        )
    if transport != MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER:
        return ManagedControlDispatchResult(
            ok=False,
            transport=MANAGED_CONTROL_TRANSPORT_NONE,
            error=MISSING_LEGACY_RUNNER_METADATA_ERROR,
        )

    runner_id = getattr(session, "source_runner_id", None)
    if runner_id is None:
        return ManagedControlDispatchResult(
            ok=False,
            transport=MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER,
            error=MISSING_LEGACY_RUNNER_METADATA_ERROR,
        )

    dispatcher = get_runner_job_dispatcher()
    result = await dispatcher.dispatch_job(
        db=db,
        owner_id=owner_id,
        runner_id=int(runner_id),
        command=command,
        timeout_secs=timeout_secs,
        commis_id=commis_id,
        run_id=run_id,
    )
    if not result.get("ok"):
        return ManagedControlDispatchResult(
            ok=False,
            transport=MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER,
            error=_runner_dispatch_error(result, failure_message),
        )

    data = result.get("data", {})
    if not isinstance(data, Mapping):
        data = {}
    return ManagedControlDispatchResult(
        ok=True,
        transport=MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER,
        data=data,
    )


def _engine_error_message(error: Any, fallback: str) -> tuple[str | None, str]:
    if isinstance(error, Mapping):
        code = str(error.get("code") or "").strip() or None
        message = str(error.get("message") or "").strip() or fallback
        return code, message
    if error:
        return None, str(error)
    return None, fallback


def _engine_command_result_data(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    result = message.get("result", {})
    if not isinstance(result, Mapping):
        return None
    data = dict(result)
    if "exit_code" not in data:
        return None
    data.setdefault("stdout", "")
    data.setdefault("stderr", "")
    return data


async def _dispatch_engine_channel(
    *,
    owner_id: int,
    session: AgentSession,
    command_type: str | None,
    payload: Mapping[str, Any] | None,
    timeout_secs: int,
    command_id: str | None = None,
) -> ManagedControlDispatchResult:
    if command_type is None:
        return ManagedControlDispatchResult(
            ok=False,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            error="Managed control command is missing command_type",
        )
    device_id = _session_device_id(session)
    if device_id is None:
        return ManagedControlDispatchResult(
            ok=False,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            error="Managed local session is missing device_id",
        )

    response = await get_machine_control_channel_registry().send_command(
        owner_id=owner_id,
        device_id=device_id,
        session_id=str(getattr(session, "id")),
        command_type=command_type,
        payload={
            "provider": str(getattr(session, "provider", "") or "").strip().lower(),
            **dict(payload or {}),
        },
        timeout_secs=timeout_secs,
        command_id=command_id,
    )
    if not response.transport_ok:
        return ManagedControlDispatchResult(
            ok=False,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            error=response.error or "Machine Agent control channel dispatch failed",
        )

    message = response.message or {}
    if message.get("ok") is True:
        data = _engine_command_result_data(message)
        if data is None:
            return ManagedControlDispatchResult(
                ok=False,
                transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
                error="Machine Agent control command returned malformed result",
            )
        return ManagedControlDispatchResult(
            ok=True,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            data=data,
        )

    code, error = _engine_error_message(message.get("error"), "Machine Agent control command failed")
    if code == "turn_ended":
        return ManagedControlDispatchResult(
            ok=True,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            data={
                "exit_code": 2,
                "stdout": "",
                "stderr": f"error_code: turn_ended\nerror_detail: {error}",
            },
        )

    return ManagedControlDispatchResult(
        ok=True,
        transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
        data={
            "exit_code": 1,
            "stdout": "",
            "stderr": error,
        },
    )
