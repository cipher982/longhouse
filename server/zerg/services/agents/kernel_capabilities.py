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

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
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

    @property
    def reply_to_live_session_available(self) -> bool:
        return bool(self.live_control_available and self.can_send_input)

    @property
    def can_queue_next_input(self) -> bool:
        return bool(self.live_control_available and self.can_send_input)

    @property
    def can_steer_active_turn(self) -> bool:
        return bool(self.live_control_available and self.control_plane in ("codex_bridge", "opencode_process"))

    @property
    def execution_home(self):
        from zerg.session_execution_home import SessionExecutionHome

        return (
            SessionExecutionHome.MANAGED_LOCAL
            if (self.live_control_available or self.host_reattach_available)
            else SessionExecutionHome.UNMANAGED_LOCAL
        )

    @property
    def managed_transport(self):
        from zerg.session_execution_home import ManagedSessionTransport

        if self.control_plane == "codex_bridge":
            return ManagedSessionTransport.CODEX_APP_SERVER
        if self.control_plane == "claude_channel_bridge":
            return ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE
        if self.control_plane == "opencode_process":
            return ManagedSessionTransport.OPENCODE_PROCESS
        if self.control_plane == "antigravity_process":
            return ManagedSessionTransport.ANTIGRAVITY_PROCESS
        return None

    @property
    def home_label(self) -> Optional[str]:
        return "On this Mac" if (self.live_control_available or self.host_reattach_available) else None


def _connection_capability_count(conn: SessionConnection) -> int:
    return int(bool(conn.can_send_input) + bool(conn.can_interrupt) + bool(conn.can_terminate) + bool(conn.can_tail_output))


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
        # Live spawned/adopted control on the host: also expose
        # ``host_reattach_available`` so attach-command generation works.
        return ("live", True, True, False, False, None)

    if is_steerable_kind and state in ("detached", "released"):
        # Process owner is gone but the control plane could be reattached.
        return ("reattach", False, True, False, False, "connection_released")

    if can_tail and state in ("attached", "degraded"):
        return ("search-only", False, False, True, False, "observe_only")

    return ("search-only", False, False, True, False, "observe_only")


def project_session_capabilities(db: Session, *, session_id) -> KernelSessionCapabilities:
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
    latest_run = None
    connections: list = []
    if thread is not None:
        latest_run = (
            db.query(SessionRun)
            .filter(SessionRun.thread_id == thread.id)
            .order_by(SessionRun.started_at.desc(), SessionRun.id.desc())
            .first()
        )
        if latest_run is not None:
            connections = db.query(SessionConnection).filter(SessionConnection.run_id == latest_run.id).all()
    return _payload_from_rows(sid=sid, thread=thread, latest_run=latest_run, connections=connections)


def _imported_payload(
    *,
    sid: str,
    thread_id: Optional[str] = None,
    run_id: Optional[str] = None,
    has_thread: bool,
    has_run: bool,
    run_ended: bool,
    best: Optional[SessionConnection],
) -> KernelSessionCapabilities:
    label, live, reattach, observe, search, reason = _label_for(has_thread=has_thread, has_run=has_run, run_ended=run_ended, best=best)
    return KernelSessionCapabilities(
        session_id=sid,
        thread_id=thread_id,
        run_id=run_id,
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


def _payload_from_rows(
    *,
    sid: str,
    thread: Optional[SessionThread],
    latest_run: Optional[SessionRun],
    connections: list[SessionConnection],
) -> KernelSessionCapabilities:
    if thread is None:
        return _imported_payload(sid=sid, has_thread=False, has_run=False, run_ended=False, best=None)
    if latest_run is None:
        return _imported_payload(sid=sid, thread_id=str(thread.id), has_thread=True, has_run=False, run_ended=False, best=None)
    best = _select_best_connection(connections)
    run_ended = latest_run.ended_at is not None
    label, live, reattach, observe, search, reason = _label_for(has_thread=True, has_run=True, run_ended=run_ended, best=best)
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
    can_send = bool(best.can_send_input) and live
    can_interrupt = bool(best.can_interrupt) and live
    can_terminate = bool(best.can_terminate) and live
    can_tail = bool(best.can_tail_output) and (live or observe)
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


def project_capabilities_bulk(db: Session, *, session_ids: list) -> dict:
    """Project capabilities for many sessions with three batched queries.

    Returns ``{session_id: KernelSessionCapabilities}``. Sessions without a
    primary thread still appear in the result with the "imported" payload.
    Replaces the per-id loop so list endpoints don't pay an N×3-query tax.
    """

    out: dict = {}
    if not session_ids:
        return out

    threads = (
        db.query(SessionThread)
        .filter(SessionThread.session_id.in_(session_ids), SessionThread.is_primary == 1)
        .order_by(SessionThread.created_at.asc(), SessionThread.id.asc())
        .all()
    )
    # Defense-in-depth: a corrupted DB might return >1 primary; keep the first.
    thread_by_session: dict = {}
    for t in threads:
        thread_by_session.setdefault(t.session_id, t)

    thread_ids = [t.id for t in thread_by_session.values()]
    runs_by_thread: dict = {}
    if thread_ids:
        runs = (
            db.query(SessionRun)
            .filter(SessionRun.thread_id.in_(thread_ids))
            .order_by(SessionRun.started_at.desc(), SessionRun.id.desc())
            .all()
        )
        for r in runs:
            runs_by_thread.setdefault(r.thread_id, r)

    run_ids = [r.id for r in runs_by_thread.values()]
    conns_by_run: dict = defaultdict(list)
    if run_ids:
        conns = db.query(SessionConnection).filter(SessionConnection.run_id.in_(run_ids)).all()
        for c in conns:
            conns_by_run[c.run_id].append(c)

    for sid in session_ids:
        thread = thread_by_session.get(sid)
        latest_run = runs_by_thread.get(thread.id) if thread is not None else None
        connections = conns_by_run.get(latest_run.id, []) if latest_run is not None else []
        out[sid] = _payload_from_rows(sid=str(sid), thread=thread, latest_run=latest_run, connections=connections)
    return out


__all__ = [
    "KernelSessionCapabilities",
    "project_session_capabilities",
    "project_capabilities_bulk",
]
