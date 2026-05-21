"""Kernel-derived capability projection for a session.

Phase 4 of the session identity kernel: one server-side function that maps
``(thread, latest run, best connection, latest run-keyed runtime_state)``
to a deterministic capability payload. Web, iOS, CLI, and ``/api/agents/*``
all read this. **No client infers managed state from ``execution_home``,
heartbeat freshness, or process liveness alone.**

A live process is not proof Longhouse can steer it. Live transcript
updates are not proof of live control. Live control requires an attached
or degraded connection with the relevant capability bit set.

See docs/specs/session-identity-kernel.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionThread


_STATE_PRIORITY = {
    "attached": 5,
    "degraded": 4,
    "detached": 3,
    "released": 2,
    "ended": 1,
}


@dataclass(frozen=True)
class KernelSessionCapabilities:
    """Deterministic capability payload for one session.

    ``control_label`` is the rolled-up bucket the UI uses; the boolean
    fields are the underlying truth that determines it.
    """

    session_id: str
    thread_id: Optional[str]
    run_id: Optional[str]
    connection_id: Optional[int]
    control_plane: Optional[str]
    connection_state: Optional[str]

    # Bucket
    control_label: str  # "live" | "reattach" | "search-only" | "imported"

    # Underlying gates
    live_control_available: bool
    host_reattach_available: bool
    observe_only: bool
    search_only: bool

    can_send_input: bool
    can_interrupt: bool
    can_terminate: bool
    can_tail_output: bool
    can_resume: bool

    # When live_control_available is False, this gives the reason: e.g.
    # "no_run", "connection_released", "process_ended", "imported_only".
    staleness_reason: Optional[str]


def _connection_capability_count(conn: SessionConnection) -> int:
    return int(
        bool(conn.can_send_input)
        + bool(conn.can_interrupt)
        + bool(conn.can_terminate)
        + bool(conn.can_tail_output)
    )


def _connection_sort_key(conn: SessionConnection) -> tuple:
    state = (conn.state or "").strip()
    state_priority = _STATE_PRIORITY.get(state, 0)
    cap_count = _connection_capability_count(conn)
    last_health = conn.last_health_at or datetime.min.replace(tzinfo=timezone.utc)
    if last_health.tzinfo is None:
        last_health = last_health.replace(tzinfo=timezone.utc)
    return (state_priority, cap_count, last_health, conn.id)


def _select_best_connection(connections: list[SessionConnection]) -> Optional[SessionConnection]:
    """Pick the best connection per spec rules.

    1. State priority: attached > degraded > detached > released > ended.
    2. Capability priority: highest count of granted flags wins.
    3. Recency: greater last_health_at wins.
    4. Final tiebreak: greater id wins.
    """

    if not connections:
        return None
    return sorted(connections, key=_connection_sort_key)[-1]


def _label_for(
    *,
    has_thread: bool,
    has_run: bool,
    run_ended: bool,
    best: Optional[SessionConnection],
) -> tuple[str, bool, bool, bool, bool, Optional[str]]:
    """Compute (control_label, live, reattach, observe_only, search_only, staleness_reason).

    Bucket selection per spec (docs/specs/session-identity-kernel.md
    "Live, reattach, observe-only — bucket gates"): the bucket is decided
    by ``state`` + ``acquisition_kind`` only. The control capability bits
    surface separately on ``can_send_input`` / ``can_interrupt`` etc — a
    spawned_control attached connection with all bits cleared is still
    "live", just with no actionable affordances.

    The ``acquisition_kind`` gate is what stops a stale ``can_send_input=1``
    on a ``log_tail`` observe_only row from projecting as live.
    """

    if not has_thread:
        return ("imported", False, False, False, True, "imported_only")
    if not has_run:
        return ("imported", False, False, False, True, "no_run")
    if best is None:
        return ("imported", False, False, False, True, "no_connection")

    state = (best.state or "").strip()
    if state == "" or state == "ended":
        # Treat unknown/empty state the same as ended: no recent control
        # truth, no live affordance.
        return ("imported", False, False, False, True, "process_ended")
    if run_ended:
        # Process is gone — even an apparently-attached row is stale.
        return ("imported", False, False, False, True, "process_ended")

    acquisition = (best.acquisition_kind or "").strip()
    is_steerable_kind = acquisition in ("spawned_control", "adopted_control")
    can_tail = bool(best.can_tail_output)

    if is_steerable_kind and state in ("attached", "degraded"):
        return ("live", True, False, False, False, None)

    if is_steerable_kind and state in ("detached", "released"):
        # Process owner is gone but the control plane could be reattached.
        return ("reattach", False, True, False, False, "connection_released")

    if can_tail and state in ("attached", "degraded"):
        return ("search-only", False, False, True, False, "observe_only")

    return ("search-only", False, False, True, False, "observe_only")


def project_session_capabilities(
    db: Session, *, session_id
) -> KernelSessionCapabilities:
    """Project capabilities for one session from kernel rows only.

    Returns a fully-populated payload even for sessions without a thread/run
    (imported pre-kernel rows). Never raises on missing kernel rows — the
    backfill is responsible for converging them, and the projection must
    return a sane "imported" payload until then.
    """

    sid = str(session_id)

    # Defense-in-depth: the unique partial index ux_threads_one_primary_per_session
    # makes this query return at most one row in a healthy DB. We still
    # use first() with a stable order so a corrupted/migrating DB doesn't
    # crash the projection — a bad primary state should degrade to a
    # deterministic "imported" payload, not a 500.
    thread = (
        db.query(SessionThread)
        .filter(SessionThread.session_id == session_id, SessionThread.is_primary == 1)
        .order_by(SessionThread.created_at.asc(), SessionThread.id.asc())
        .first()
    )
    if thread is None:
        label, live, reattach, observe, search, reason = _label_for(
            has_thread=False, has_run=False, run_ended=False, best=None
        )
        return KernelSessionCapabilities(
            session_id=sid,
            thread_id=None,
            run_id=None,
            connection_id=None,
            control_plane=None,
            connection_state=None,
            control_label=label,
            live_control_available=live,
            host_reattach_available=reattach,
            observe_only=observe,
            search_only=search,
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            can_tail_output=False,
            can_resume=False,
            staleness_reason=reason,
        )

    latest_run = (
        db.query(SessionRun)
        .filter(SessionRun.thread_id == thread.id)
        .order_by(SessionRun.started_at.desc(), SessionRun.id.desc())
        .first()
    )

    if latest_run is None:
        label, live, reattach, observe, search, reason = _label_for(
            has_thread=True, has_run=False, run_ended=False, best=None
        )
        return KernelSessionCapabilities(
            session_id=sid,
            thread_id=str(thread.id),
            run_id=None,
            connection_id=None,
            control_plane=None,
            connection_state=None,
            control_label=label,
            live_control_available=live,
            host_reattach_available=reattach,
            observe_only=observe,
            search_only=search,
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            can_tail_output=False,
            can_resume=False,
            staleness_reason=reason,
        )

    connections = (
        db.query(SessionConnection)
        .filter(SessionConnection.run_id == latest_run.id)
        .all()
    )
    best = _select_best_connection(connections)
    run_ended = latest_run.ended_at is not None

    label, live, reattach, observe, search, reason = _label_for(
        has_thread=True, has_run=True, run_ended=run_ended, best=best
    )

    if best is None:
        return KernelSessionCapabilities(
            session_id=sid,
            thread_id=str(thread.id),
            run_id=str(latest_run.id),
            connection_id=None,
            control_plane=None,
            connection_state=None,
            control_label=label,
            live_control_available=live,
            host_reattach_available=reattach,
            observe_only=observe,
            search_only=search,
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            can_tail_output=False,
            can_resume=False,
            staleness_reason=reason,
        )

    # Capability bits surface from the best connection only when the bucket
    # actually grants control. An observe_only "search-only" session never
    # exposes can_send_input even if the row carries a stale 1.
    can_send = bool(best.can_send_input) and live
    can_interrupt = bool(best.can_interrupt) and live
    can_terminate = bool(best.can_terminate) and live
    can_tail = bool(best.can_tail_output)
    can_resume = bool(best.can_resume) and (live or reattach)

    return KernelSessionCapabilities(
        session_id=sid,
        thread_id=str(thread.id),
        run_id=str(latest_run.id),
        connection_id=best.id,
        control_plane=best.control_plane,
        connection_state=best.state,
        control_label=label,
        live_control_available=live,
        host_reattach_available=reattach,
        observe_only=observe,
        search_only=search,
        can_send_input=can_send,
        can_interrupt=can_interrupt,
        can_terminate=can_terminate,
        can_tail_output=can_tail,
        can_resume=can_resume,
        staleness_reason=reason,
    )


def project_capabilities_bulk(
    db: Session, *, session_ids: list
) -> dict:
    """Project capabilities for many sessions in one shot.

    Returns ``{session_id: KernelSessionCapabilities}``. Sessions without a
    primary thread still appear in the result with the "imported" payload.
    """

    out: dict = {}
    for sid in session_ids:
        out[sid] = project_session_capabilities(db, session_id=sid)
    return out


__all__ = [
    "KernelSessionCapabilities",
    "project_session_capabilities",
    "project_capabilities_bulk",
]
