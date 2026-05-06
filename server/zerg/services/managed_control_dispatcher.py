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
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.session_execution_home import SessionExecutionHome

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


def select_managed_control_transport(session: AgentSession | None) -> str | None:
    """Return the explicit control transport for a managed session.

    Phase 1 preserves existing behavior: sessions with legacy Runner metadata
    use Runner-backed dispatch; sessions without it have no remote control
    transport until the Machine Agent channel lands.
    """

    if session is None:
        return None
    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return None
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


async def dispatch_managed_control_command(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    command: str,
    timeout_secs: int,
    commis_id: str | None = None,
    run_id: str | None = None,
    failure_message: str = "Failed to dispatch managed control command",
) -> ManagedControlDispatchResult:
    """Dispatch one managed-control command through the selected transport."""

    transport = select_managed_control_transport(session)
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
