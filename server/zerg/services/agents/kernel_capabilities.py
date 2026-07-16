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
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.services.managed_control_state import DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS
from zerg.services.managed_provider_contracts import contract_for_control_plane
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.managed_provider_contracts import managed_transport_for_control_plane
from zerg.services.managed_provider_contracts import provider_for_control_plane
from zerg.services.managed_provider_contracts import steer_control_planes
from zerg.utils.time import normalize_utc

_STATE_PRIORITY = {
    "attached": 5,
    "degraded": 4,
    "detached": 3,
    "released": 2,
    "ended": 1,
}

_STEER_CONTROL_PLANES = steer_control_planes()

_CONTROL_ACQUISITION_KINDS = ("spawned_control", "adopted_control")

_MANAGED_CONTROL_LEASE_TTL = timedelta(milliseconds=DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS)


def _effective_connection_state(best: Optional[SessionConnection], now: datetime) -> str:
    """Return the freshness-clamped connection state for ``best``.

    ``live_control_available`` must mean "an observer wrote attached/degraded
    from a ready lease within the lease TTL". The launcher and reconciler
    stamp ``last_health_at`` whenever they observe a live channel, so a stale
    or NULL ``last_health_at`` on an ``attached``/``degraded`` row means no
    observer has confirmed readiness recently (slept laptop, dead engine,
    or a legacy birth-time optimistic row that was never promoted). Demote it
    to ``detached`` at read time so the badge degrades without a background
    job. Non-live states pass through unchanged.
    """

    if best is None:
        return ""
    state = (best.state or "").strip()
    if state not in ("attached", "degraded"):
        return state
    last_health = best.last_health_at
    if last_health is None:
        return "detached"
    if last_health.tzinfo is None:
        last_health = last_health.replace(tzinfo=timezone.utc)
    if now - last_health > _MANAGED_CONTROL_LEASE_TTL:
        return "detached"
    return state


def thread_ever_had_managed_control(db: Session, *, thread_id) -> bool:
    """True if any run on this thread ever held a Longhouse-owned control path.

    Managed launches record a connection with ``acquisition_kind`` in
    (spawned_control, adopted_control); imported/unmanaged ingest records at most
    ``observe_only`` (or no connection). State is ignored on purpose — a closed
    managed session's connection is ``released``/``ended`` but it was still
    managed, which is exactly what makes it resumable.

    This is the sound managed fingerprint: it cannot be spoofed by thread-alias
    backfill the way ``provider_session_id == session.id`` can, because backfill
    only ever writes ``observe_only`` connections.
    """

    return (
        db.query(SessionConnection.id)
        .join(SessionRun, SessionConnection.run_id == SessionRun.id)
        .filter(SessionRun.thread_id == thread_id)
        .filter(SessionConnection.acquisition_kind.in_(_CONTROL_ACQUISITION_KINDS))
        .limit(1)
        .first()
    ) is not None


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

    # Durable turn execution is independent of an active connection. Console
    # uses these fields while Helm continues to use the connection grants above.
    turn_state: str = "idle"
    can_start_turn: bool = False
    start_turn_blocked_by: Optional[str] = None
    can_interrupt_active_turn: bool = False

    # When live_control_available is False, this gives the reason: e.g.
    # "no_run", "connection_released", "process_ended", "imported_only".
    staleness_reason: Optional[str] = None

    # Stable identity for the acquired connection lease. Health renewals move
    # freshness clocks but must not manufacture a new lease generation.
    lease_generation: Optional[str] = None
    control_owned: bool = False
    run_started_at: Optional[datetime] = None
    run_ended_at: Optional[datetime] = None
    run_end_reason: Optional[str] = None

    @property
    def reply_to_live_session_available(self) -> bool:
        return bool(self.live_control_available and self.can_send_input)

    @property
    def can_queue_next_input(self) -> bool:
        return bool(self.live_control_available and self.can_send_input)

    @property
    def can_steer_active_turn(self) -> bool:
        control_plane = (self.control_plane or "").strip()
        return bool(self.live_control_available and self.can_send_input and control_plane in _STEER_CONTROL_PLANES)

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
        return managed_transport_for_control_plane(self.control_plane)

    @property
    def home_label(self) -> Optional[str]:
        return "On this Mac" if (self.live_control_available or self.host_reattach_available) else None


def project_console_turn_capabilities(
    capabilities: KernelSessionCapabilities,
    *,
    closed: bool,
    execution_target_available: bool,
    turn_state: str | None,
    machine_online: bool = True,
    adapter_available: bool = True,
) -> KernelSessionCapabilities:
    """Project Console's durable turn action without manufacturing a live lease."""

    normalized_state = str(turn_state or "idle").strip().lower()
    if normalized_state not in {"idle", "queued", "starting", "active", "draining"}:
        normalized_state = "idle"
    blocked_by = "session_closed" if closed else None
    if blocked_by is None and not execution_target_available:
        blocked_by = "execution_target_missing"
    if blocked_by is None and not machine_online:
        blocked_by = "machine_offline"
    if blocked_by is None and not adapter_available:
        blocked_by = "adapter_unavailable"
    can_start = blocked_by is None
    return replace(
        capabilities,
        control_label="console",
        live_control_available=False,
        host_reattach_available=False,
        observe_only=False,
        search_only=False,
        can_send_input=can_start,
        can_resume=False,
        control_owned=True,
        staleness_reason=blocked_by,
        turn_state=normalized_state,
        can_start_turn=can_start,
        start_turn_blocked_by=blocked_by,
        can_interrupt_active_turn=(normalized_state in {"starting", "active", "draining"} and capabilities.can_interrupt),
    )


def _connection_capability_count(conn: SessionConnection) -> int:
    capabilities = (
        conn.can_send_input,
        conn.can_interrupt,
        conn.can_terminate,
        conn.can_tail_output,
    )
    return sum(1 for capability in capabilities if capability)


def _control_plane_supports(control_plane: str | None, operation: str) -> bool:
    """Whether a known managed provider contract allows ``operation``.

    Persisted connection bits are runtime observations, not provider contracts.
    For known managed control planes, clamp those bits to the provider-level
    contract so a bad row cannot make session capabilities claim an unsupported
    action. Unknown planes keep legacy behavior because there is no provider
    contract to reconcile against.
    """

    contract = contract_for_control_plane(control_plane)
    if contract is None:
        provider = provider_for_control_plane(control_plane)
        contract = contract_for_provider(provider)
    if contract is None:
        return True
    return contract.supports_contract_operation(operation)


def _connection_sort_key(conn: SessionConnection, now: datetime) -> tuple:
    # Rank on the freshness-clamped state so a stale/NULL-health attached row
    # does not outrank a genuinely-fresh degraded/detached row and then get
    # demoted after selection (which would mask the better connection).
    state = _effective_connection_state(conn, now)
    state_priority = _STATE_PRIORITY.get(state, 0)
    cap_count = _connection_capability_count(conn)
    last_health = conn.last_health_at or datetime.min.replace(tzinfo=timezone.utc)
    if last_health.tzinfo is None:
        last_health = last_health.replace(tzinfo=timezone.utc)
    return (state_priority, cap_count, last_health, conn.id)


def _select_best_connection(connections: list[SessionConnection], now: datetime) -> Optional[SessionConnection]:
    """Pick the best connection per spec rules.

    1. State priority (freshness-clamped): attached > degraded > detached >
       released > ended.
    2. Capability priority: highest count of granted flags wins.
    3. Recency: greater last_health_at wins.
    4. Final tiebreak: greater id wins.
    """

    if not connections:
        return None
    return sorted(connections, key=lambda conn: _connection_sort_key(conn, now))[-1]


def _label_for(
    *,
    has_thread: bool,
    has_run: bool,
    run_ended: bool,
    best: Optional[SessionConnection],
    now: datetime,
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

    raw_state = (best.state or "").strip()
    if raw_state == "" or raw_state == "ended":
        # Treat unknown/empty state the same as ended: no recent control
        # truth, no live affordance.
        return ("imported", False, False, False, True, "process_ended")
    if run_ended:
        # Process is gone — even an apparently-attached row is stale.
        return ("imported", False, False, False, True, "process_ended")

    # Read-time freshness clamp: an attached/degraded row whose health
    # timestamp is missing or older than the lease TTL has not been confirmed
    # ready by any observer recently, so it is demoted to detached here. This
    # makes ``live_control_available`` mean "observed ready within the TTL".
    state = _effective_connection_state(best, now)
    stale_clamped = state != raw_state

    acquisition = (best.acquisition_kind or "").strip()
    is_steerable_kind = acquisition in ("spawned_control", "adopted_control")
    can_tail = bool(best.can_tail_output)

    if is_steerable_kind and state in ("attached", "degraded"):
        # Live spawned/adopted control on the host: also expose
        # ``host_reattach_available`` so attach-command generation works.
        return ("live", True, True, False, False, None)

    if is_steerable_kind and state in ("detached", "released"):
        # Process owner is gone (or freshness lapsed) but the control plane
        # could be reattached.
        reason = "control_stale" if stale_clamped else "connection_released"
        return ("reattach", False, True, False, False, reason)

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
    return _payload_from_rows(
        sid=sid,
        thread=thread,
        latest_run=latest_run,
        connections=connections,
        now=datetime.now(timezone.utc),
    )


def _imported_payload(
    *,
    sid: str,
    thread_id: Optional[str] = None,
    run_id: Optional[str] = None,
    has_thread: bool,
    has_run: bool,
    run_ended: bool,
    best: Optional[SessionConnection],
    now: datetime,
) -> KernelSessionCapabilities:
    label, live, reattach, observe, search, reason = _label_for(
        has_thread=has_thread,
        has_run=has_run,
        run_ended=run_ended,
        best=best,
        now=now,
    )
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
    now: datetime,
) -> KernelSessionCapabilities:
    if thread is None:
        return _imported_payload(sid=sid, has_thread=False, has_run=False, run_ended=False, best=None, now=now)
    if latest_run is None:
        return _imported_payload(
            sid=sid,
            thread_id=str(thread.id),
            has_thread=True,
            has_run=False,
            run_ended=False,
            best=None,
            now=now,
        )
    best = _select_best_connection(connections, now)
    run_ended = latest_run.ended_at is not None
    label, live, reattach, observe, search, reason = _label_for(
        has_thread=True,
        has_run=True,
        run_ended=run_ended,
        best=best,
        now=now,
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
            run_started_at=latest_run.started_at,
            run_ended_at=latest_run.ended_at,
            run_end_reason=latest_run.exit_status,
        )
    can_send = (
        bool(best.can_send_input)
        and live
        and _control_plane_supports(
            best.control_plane,
            "send_input",
        )
    )
    can_interrupt = (
        bool(best.can_interrupt)
        and live
        and _control_plane_supports(
            best.control_plane,
            "interrupt",
        )
    )
    can_terminate = (
        bool(best.can_terminate)
        and live
        and _control_plane_supports(
            best.control_plane,
            "terminate",
        )
    )
    can_tail = (
        bool(best.can_tail_output)
        and (live or observe)
        and _control_plane_supports(
            best.control_plane,
            "tail_output",
        )
    )
    can_resume = (
        bool(best.can_resume)
        and (live or reattach)
        and _control_plane_supports(
            best.control_plane,
            "reattach",
        )
    )
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
        lease_generation=(
            f"{best.id}:{normalize_utc(best.acquired_at).isoformat()}"
            if best.id is not None and normalize_utc(best.acquired_at) is not None
            else str(best.id)
            if best.id is not None
            else None
        ),
        control_owned=best.acquisition_kind in _CONTROL_ACQUISITION_KINDS,
        run_started_at=latest_run.started_at,
        run_ended_at=latest_run.ended_at,
        run_end_reason=latest_run.exit_status,
    )


def project_capabilities_from_rows(
    *,
    session_id,
    thread,
    latest_run,
    connections: list,
    now: datetime | None = None,
) -> KernelSessionCapabilities:
    """Project canonical capability truth from kernel-compatible ORM rows.

    The bounded live catalog mirrors the thread/run/connection kernel using
    separate ORM classes.  Keep the capability rules in one place while
    allowing either store's rows to feed the same deterministic projector.
    """

    return _payload_from_rows(
        sid=str(session_id),
        thread=thread,
        latest_run=latest_run,
        connections=connections,
        now=now or datetime.now(timezone.utc),
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

    now = datetime.now(timezone.utc)
    for sid in session_ids:
        thread = thread_by_session.get(sid)
        latest_run = runs_by_thread.get(thread.id) if thread is not None else None
        connections = conns_by_run.get(latest_run.id, []) if latest_run is not None else []
        out[sid] = _payload_from_rows(
            sid=str(sid),
            thread=thread,
            latest_run=latest_run,
            connections=connections,
            now=now,
        )
    return out


__all__ = [
    "KernelSessionCapabilities",
    "project_console_turn_capabilities",
    "project_capabilities_from_rows",
    "project_session_capabilities",
    "project_capabilities_bulk",
]
