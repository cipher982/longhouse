"""Shared managed-local control helpers.

This module keeps tmux-backed local session control in one place so the
session-chat route and Loop actions use the same transport semantics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_local_runtime import mark_managed_local_input_sent
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome

MANAGED_LOCAL_EVENT_TIMEOUT_SECS = 150.0
MANAGED_LOCAL_POLL_INTERVAL_SECS = 1.0
MANAGED_LOCAL_STABLE_POLLS = 1


@dataclass(frozen=True)
class ManagedLocalSendResult:
    ok: bool
    exit_code: int | None = None
    error: str | None = None
    baseline_event_id: int | None = None


def get_managed_local_latest_event_id(*, db: Session, session_id: UUID) -> int:
    """Return the latest stored event id for a managed-local session."""
    return int(AgentsStore(db).get_latest_event_id(session_id) or 0)


def _fetch_managed_local_events_since(*, db_bind, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id > after_event_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


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


async def send_text_to_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 15,
) -> ManagedLocalSendResult:
    """Send text into a tmux-backed managed-local session.

    Returns a normalized result so callers do not need to know the runner
    dispatch envelope details.
    """

    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalSendResult(ok=False, error="Session is not managed_local")
    if str(getattr(session, "provider", "") or "").strip().lower() == "codex":
        return ManagedLocalSendResult(
            ok=False,
            error="Managed-local Codex is terminal-driven right now; attach locally instead of sending web input.",
        )
    if str(getattr(session, "managed_transport", "") or "").strip() != ManagedSessionTransport.TMUX.value:
        return ManagedLocalSendResult(ok=False, error="Managed local session does not use tmux transport")
    if not getattr(session, "source_runner_id", None):
        return ManagedLocalSendResult(ok=False, error="Managed local session is missing source runner metadata")
    if not getattr(session, "managed_session_name", None):
        return ManagedLocalSendResult(ok=False, error="Managed local session is missing tmux metadata")

    baseline_event_id = get_managed_local_latest_event_id(db=db, session_id=session.id)
    dispatcher = get_runner_job_dispatcher()
    result = await dispatcher.dispatch_job(
        db=db,
        owner_id=owner_id,
        runner_id=int(session.source_runner_id),
        command=build_tmux_send_text_command(
            session_name=str(session.managed_session_name),
            text=text,
            tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
        ),
        timeout_secs=timeout_secs,
        commis_id=commis_id,
        run_id=None,
    )

    if not result.get("ok"):
        return ManagedLocalSendResult(
            ok=False,
            baseline_event_id=baseline_event_id,
            error=str(result.get("error", {}).get("message", "Failed to send text to managed local session")),
        )

    data = result.get("data", {})
    exit_code = int(data.get("exit_code", 1))
    if exit_code != 0:
        detail = (data.get("stderr") or "").strip() or (data.get("stdout") or "").strip()
        return ManagedLocalSendResult(
            ok=False,
            exit_code=exit_code,
            baseline_event_id=baseline_event_id,
            error=detail or "Managed local send-text command failed",
        )

    mark_managed_local_input_sent(
        db,
        session=session,
        dedupe_suffix=str(commis_id or ""),
    )
    return ManagedLocalSendResult(ok=True, exit_code=0, baseline_event_id=baseline_event_id)
