"""Managed-local runtime signal helpers for Timeline.

These helpers emit provider-agnostic runtime events at the exact points where
Longhouse knows something concrete about a tmux-backed managed-local session.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.session_execution_home import SessionExecutionHome

MANAGED_LOCAL_RUNTIME_SOURCE = "managed_local_transport"


def _is_managed_local_session(session: AgentSession) -> bool:
    return str(getattr(session, "execution_home", "") or "").strip() == SessionExecutionHome.MANAGED_LOCAL.value


def _emit_managed_local_phase_signal(
    db: Session,
    *,
    session: AgentSession,
    phase: str,
    dedupe_key: str,
    occurred_at: datetime | None = None,
) -> None:
    if not _is_managed_local_session(session):
        return

    signal_at = occurred_at or datetime.now(timezone.utc)
    runtime_key = runtime_key_for_session(str(session.provider or "claude"), str(session.id))
    ingest_runtime_events(
        db,
        [
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session.id,
                provider=str(session.provider or "claude"),
                device_id=str(session.device_id or session.source_runner_name or "") or None,
                source=MANAGED_LOCAL_RUNTIME_SOURCE,
                kind="phase_signal",
                phase=phase,
                tool_name=None,
                occurred_at=signal_at,
                freshness_ms=phase_freshness_ms(phase),
                dedupe_key=dedupe_key,
                payload={"managed_transport": getattr(session, "managed_transport", None)},
            )
        ],
    )


def _upsert_managed_local_presence(
    db: Session,
    *,
    session: AgentSession,
    state: str,
    occurred_at: datetime | None = None,
) -> None:
    if not _is_managed_local_session(session):
        return

    updated_at = occurred_at or datetime.now(timezone.utc)
    session_id = str(session.id)
    presence = db.query(SessionPresence).filter(SessionPresence.session_id == session_id).one_or_none()
    if presence is None:
        db.add(
            SessionPresence(
                session_id=session_id,
                state=state,
                tool_name=None,
                cwd=session.cwd,
                project=session.project,
                provider=str(session.provider or "claude"),
                updated_at=updated_at,
            )
        )
        return

    presence.state = state
    presence.tool_name = None
    presence.cwd = session.cwd
    presence.project = session.project
    presence.provider = str(session.provider or presence.provider or "claude")
    presence.updated_at = updated_at


def mark_managed_local_session_launched(db: Session, *, session: AgentSession) -> None:
    _upsert_managed_local_presence(db, session=session, state="idle")
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="idle",
        dedupe_key=f"managed-local-launch:{session.id}",
    )


def mark_managed_local_input_sent(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="thinking",
        dedupe_key=f"managed-local-send:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


def mark_managed_local_turn_idle(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="idle",
        dedupe_key=f"managed-local-idle:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


def mark_managed_local_turn_needs_user(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="needs_user",
        dedupe_key=f"managed-local-needs-user:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


def mark_managed_local_turn_blocked(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="blocked",
        dedupe_key=f"managed-local-blocked:{session.id}:{dedupe_suffix or uuid4().hex}",
    )
