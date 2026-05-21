"""Shared kernel-row seeders for tests that previously relied on legacy
``execution_home``/``managed_transport`` columns to mark a session as
managed-local. After Phase 4 (B) the kernel projection is the only source
of truth, so tests that need a "live managed" or "reattachable" session
must seed real ``session_runs`` and ``session_connections`` rows.

These helpers do NOT touch ``execution_home`` / ``managed_transport`` — by
design, those legacy columns are no longer authoritative.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread


def _ensure_primary_thread(db: Session, session: AgentSession) -> SessionThread:
    thread = (
        db.query(SessionThread)
        .filter(SessionThread.session_id == session.id, SessionThread.is_primary == 1)
        .one_or_none()
    )
    if thread is not None:
        return thread
    thread = SessionThread(
        session_id=session.id,
        provider=session.provider,
        branch_kind="root",
        is_primary=1,
    )
    db.add(thread)
    db.flush()
    if session.primary_thread_id != thread.id:
        session.primary_thread_id = thread.id
    return thread


def seed_managed_kernel_rows(
    db: Session,
    session: AgentSession,
    *,
    control_plane: str = "claude_channel_bridge",
    state: str = "attached",
    can_send_input: bool = True,
    can_interrupt: bool = True,
    can_terminate: bool = False,
    can_tail_output: bool = True,
    can_resume: bool = False,
    acquisition_kind: str = "spawned_control",
    launch_origin: str = "longhouse_spawned",
    host_id: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> tuple[SessionThread, SessionRun, SessionConnection]:
    """Seed a primary thread + open run + connection for ``session``.

    Defaults to a fully-live managed-local Claude channel: bridge attached,
    can_send_input + can_interrupt + can_tail_output. Tests that need a
    different bucket override the ``state`` / capability flags / control
    plane (e.g. ``state="detached"`` for the reattach bucket, or
    ``control_plane="codex_bridge"`` for Codex steer).
    """

    thread = _ensure_primary_thread(db, session)
    now = datetime.now(timezone.utc)
    run = SessionRun(
        thread_id=thread.id,
        provider=thread.provider or session.provider,
        host_id=host_id or getattr(session, "device_id", None),
        cwd=getattr(session, "cwd", None),
        launch_origin=launch_origin,
        started_at=started_at or now,
        ended_at=ended_at,
    )
    db.add(run)
    db.flush()
    conn = SessionConnection(
        run_id=run.id,
        control_plane=control_plane,
        acquisition_kind=acquisition_kind,
        state=state,
        can_send_input=int(can_send_input),
        can_interrupt=int(can_interrupt),
        can_terminate=int(can_terminate),
        can_tail_output=int(can_tail_output),
        can_resume=int(can_resume),
        last_health_at=now,
    )
    db.add(conn)
    db.flush()
    return thread, run, conn


__all__ = ["seed_managed_kernel_rows"]
