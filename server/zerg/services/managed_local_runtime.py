"""Managed-local runtime signal helpers for Timeline.

These helpers emit provider-agnostic runtime events at the exact points where
Longhouse knows something concrete about a managed-local session lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.services.managed_local_tmux import build_tmux_pane_status_command
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.write_serializer import get_write_serializer
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome

MANAGED_LOCAL_RUNTIME_SOURCE = "managed_local_transport"
MANAGED_LOCAL_RECONCILE_SOURCE = "managed_local_reconcile"


def _is_managed_local_session(session: AgentSession) -> bool:
    return str(getattr(session, "execution_home", "") or "").strip() == SessionExecutionHome.MANAGED_LOCAL.value


def _is_managed_local_tmux_session(session: AgentSession) -> bool:
    return _is_managed_local_session(session) and str(getattr(session, "managed_transport", "") or "").strip() == (
        ManagedSessionTransport.TMUX.value
    )


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def reconcile_managed_local_tmux_terminal(
    db: Session,
    *,
    session: AgentSession,
    occurred_at: datetime | None = None,
    terminal_state: str = "finished",
    reason: str,
    exit_code: int | None = None,
) -> None:
    if not _is_managed_local_tmux_session(session):
        return

    signal_at = occurred_at or datetime.now(timezone.utc)
    existing_ended_at = _normalize_utc(getattr(session, "ended_at", None))
    if existing_ended_at is None or existing_ended_at < signal_at:
        session.ended_at = signal_at
    ingest_runtime_events(
        db,
        [
            RuntimeEventIngest(
                runtime_key=runtime_key_for_session(str(session.provider or "claude"), str(session.id)),
                session_id=session.id,
                provider=str(session.provider or "claude"),
                device_id=str(session.device_id or session.source_runner_name or "") or None,
                source=MANAGED_LOCAL_RECONCILE_SOURCE,
                kind="terminal_signal",
                phase=None,
                tool_name=None,
                occurred_at=signal_at,
                freshness_ms=0,
                dedupe_key=f"managed-local-terminal:{session.id}:{reason}",
                payload={
                    "terminal_state": terminal_state,
                    "reason": reason,
                    "exit_code": exit_code,
                    "managed_transport": getattr(session, "managed_transport", None),
                },
            )
        ],
    )


def parse_managed_local_tmux_pane_status(stdout: str | None) -> tuple[bool, int | None, str | None]:
    raw = str(stdout or "").strip()
    if not raw:
        return False, None, None
    dead_raw, dead_status_raw, current_command = (raw.split("\t", 2) + ["", "", ""])[:3]
    is_dead = dead_raw.strip() == "1"
    dead_status: int | None = None
    dead_status_raw = dead_status_raw.strip()
    if dead_status_raw:
        try:
            dead_status = int(dead_status_raw)
        except ValueError:
            dead_status = None
    return is_dead, dead_status, (current_command.strip() or None)


def mark_managed_local_session_launched(db: Session, *, session: AgentSession) -> None:
    _upsert_managed_local_presence(db, session=session, state="idle")
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="idle",
        dedupe_key=f"managed-local-launch:{session.id}",
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


async def persist_managed_local_phase_signal(
    db: Session,
    *,
    session: AgentSession,
    phase: str,
    dedupe_key: str,
    occurred_at: datetime | None = None,
) -> None:
    if not _is_managed_local_session(session):
        return

    ws = get_write_serializer()

    def _do(wdb: Session) -> None:
        _emit_managed_local_phase_signal(
            wdb,
            session=session,
            phase=phase,
            dedupe_key=dedupe_key,
            occurred_at=occurred_at,
        )

    await ws.execute_or_direct(_do, db, label="runtime-events")


async def persist_managed_local_turn_idle(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    await persist_managed_local_phase_signal(
        db,
        session=session,
        phase="idle",
        dedupe_key=f"managed-local-idle:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


async def persist_managed_local_turn_needs_user(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    await persist_managed_local_phase_signal(
        db,
        session=session,
        phase="needs_user",
        dedupe_key=f"managed-local-needs-user:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


async def persist_managed_local_turn_blocked(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    await persist_managed_local_phase_signal(
        db,
        session=session,
        phase="blocked",
        dedupe_key=f"managed-local-blocked:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


async def reconcile_managed_local_tmux_sessions(
    db: Session,
    *,
    sessions: list[AgentSession],
    owner_id: int | None,
    occurred_at: datetime | None = None,
) -> set[str]:
    if not owner_id:
        return set()

    dispatcher = get_runner_job_dispatcher()
    signal_at = occurred_at or datetime.now(timezone.utc)
    reconciled: set[str] = set()

    for session in sessions:
        if not _is_managed_local_tmux_session(session):
            continue
        if getattr(session, "source_runner_id", None) is None:
            continue
        session_name = str(getattr(session, "managed_session_name", "") or "").strip()
        if not session_name:
            continue

        try:
            probe = await dispatcher.dispatch_job(
                db=db,
                owner_id=owner_id,
                runner_id=int(session.source_runner_id),
                command=build_tmux_pane_status_command(
                    session_name=session_name,
                    tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
                ),
                timeout_secs=10,
                commis_id=None,
                run_id=None,
            )
        except Exception:
            continue

        if not probe.get("ok"):
            continue
        probe_data = probe.get("data", {})
        try:
            exit_code = int(probe_data.get("exit_code", 1))
        except (TypeError, ValueError):
            exit_code = 1
        if exit_code != 0:
            reconcile_managed_local_tmux_terminal(
                db,
                session=session,
                occurred_at=signal_at,
                reason="tmux_session_missing",
            )
            reconciled.add(str(session.id))
            continue

        pane_dead, pane_dead_status, _pane_command = parse_managed_local_tmux_pane_status(probe_data.get("stdout"))
        if not pane_dead:
            continue

        reconcile_managed_local_tmux_terminal(
            db,
            session=session,
            occurred_at=signal_at,
            reason="provider_clean_exit" if pane_dead_status in {None, 0} else "provider_nonzero_exit",
            exit_code=pane_dead_status,
        )
        reconciled.add(str(session.id))

    if reconciled:
        db.commit()

    return reconciled
