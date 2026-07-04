"""Managed-session control dispatch through the Machine Agent channel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Mapping

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.managed_provider_contracts import machine_control_capability_for_command

MANAGED_CONTROL_COMMAND_INTERRUPT = "session.interrupt"
MANAGED_CONTROL_COMMAND_SEND_TEXT = "session.send_text"
MANAGED_CONTROL_COMMAND_STEER_TEXT = "session.steer_text"
MANAGED_CONTROL_COMMAND_ANSWER_PAUSE = "session.answer_pause"
MANAGED_CONTROL_COMMAND_TERMINATE = "session.terminate"
MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL = "engine_channel"
MANAGED_CONTROL_TRANSPORT_NONE = "none"
MANAGED_CONTROL_UNAVAILABLE_ERROR = "Managed control channel is not connected or does not advertise this capability"


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
    provider = str(getattr(session, "provider", "") or "").strip().lower()
    contract = contract_for_provider(provider)
    if contract is None:
        return False
    # Minimal non-ORM fixtures may still provide a transport for dispatcher
    # unit tests. Real AgentSession rows derive transport from the provider
    # contract and machine-control capability.
    transport = ""
    if not isinstance(session, AgentSession):
        transport = str(getattr(session, "managed_transport", "") or "").strip()
    if transport and transport != contract.managed_transport.value:
        return False
    capability = machine_control_capability_for_command(provider, command_type)
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

    Managed-session live control is delivered only through the Machine Agent
    engine channel. Runner metadata is not a managed-control transport.
    """

    if session is None:
        return None
    if _session_uses_engine_control(session, owner_id=owner_id, command_type=command_type):
        return MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
    return None


def _engine_command_id(
    *,
    session: AgentSession,
    command_type: str | None,
    request_id: str | None,
    run_id: str | None,
) -> str | None:
    seed = str(request_id or run_id or "").strip()
    command = str(command_type or "").strip()
    if not seed or not command:
        return None
    return f"managed-control:{getattr(session, 'id')}:{command}:{seed}"


async def dispatch_managed_control_command(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    timeout_secs: int,
    command_type: str | None = None,
    payload: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
) -> ManagedControlDispatchResult:
    """Dispatch one managed-control command through the Machine Agent channel."""
    del db

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
                request_id=request_id,
                run_id=run_id,
            ),
        )
    return ManagedControlDispatchResult(
        ok=False,
        transport=MANAGED_CONTROL_TRANSPORT_NONE,
        error=MANAGED_CONTROL_UNAVAILABLE_ERROR,
    )


def _engine_error_message(error: Any, default_message: str) -> tuple[str | None, str]:
    if isinstance(error, Mapping):
        code = str(error.get("code") or "").strip() or None
        message = str(error.get("message") or "").strip() or default_message
        return code, message
    if error:
        return None, str(error)
    return None, default_message


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
