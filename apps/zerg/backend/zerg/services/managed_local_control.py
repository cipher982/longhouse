"""Shared managed-local control helpers.

This module keeps tmux-backed local session control in one place so the
session-chat route and Loop actions use the same transport semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome


@dataclass(frozen=True)
class ManagedLocalSendResult:
    ok: bool
    exit_code: int | None = None
    error: str | None = None


async def send_text_to_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 15,
) -> ManagedLocalSendResult:
    """Send text into a tmux-backed managed-local Claude session.

    Returns a normalized result so callers do not need to know the runner
    dispatch envelope details.
    """

    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalSendResult(ok=False, error="Session is not managed_local")
    if str(getattr(session, "managed_transport", "") or "").strip() != ManagedSessionTransport.TMUX.value:
        return ManagedLocalSendResult(ok=False, error="Managed local session does not use tmux transport")
    if not getattr(session, "source_runner_id", None):
        return ManagedLocalSendResult(ok=False, error="Managed local session is missing source runner metadata")
    if not getattr(session, "managed_session_name", None):
        return ManagedLocalSendResult(ok=False, error="Managed local session is missing tmux metadata")

    dispatcher = get_runner_job_dispatcher()
    result = await dispatcher.dispatch_job(
        db=db,
        owner_id=owner_id,
        runner_id=int(session.source_runner_id),
        command=build_tmux_send_text_command(
            session_name=str(session.managed_session_name),
            text=text,
        ),
        timeout_secs=timeout_secs,
        commis_id=commis_id,
        run_id=None,
    )

    if not result.get("ok"):
        return ManagedLocalSendResult(
            ok=False,
            error=str(result.get("error", {}).get("message", "Failed to send text to managed local session")),
        )

    data = result.get("data", {})
    exit_code = int(data.get("exit_code", 1))
    if exit_code != 0:
        detail = (data.get("stderr") or "").strip() or (data.get("stdout") or "").strip()
        return ManagedLocalSendResult(
            ok=False,
            exit_code=exit_code,
            error=detail or "Managed local send-text command failed",
        )

    return ManagedLocalSendResult(ok=True, exit_code=0)
