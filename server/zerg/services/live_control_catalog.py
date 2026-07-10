"""Hot session identity and capability projection for catalog-mode control."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread


@dataclass(frozen=True)
class LiveControlSession:
    """The bounded AgentSession shape required by machine-control dispatch."""

    id: UUID
    provider: str
    device_id: str | None
    device_name: str | None
    cwd: str | None
    project: str | None
    git_repo: str | None
    git_branch: str | None
    ended_at: object | None
    loop_mode: str
    permission_mode: str
    primary_thread_id: UUID | None


def load_live_control_session(db: Session, session_id: UUID | str) -> LiveControlSession | None:
    key = str(session_id)
    row = db.query(LiveSessionCatalog).filter(LiveSessionCatalog.session_id == key).first()
    if row is None:
        return None
    try:
        parsed_id = UUID(str(row.session_id))
    except ValueError:
        return None
    thread_id = None
    if row.primary_thread_id:
        try:
            thread_id = UUID(str(row.primary_thread_id))
        except ValueError:
            thread_id = None
    return LiveControlSession(
        id=parsed_id,
        provider=str(row.provider or "unknown"),
        device_id=str(row.device_id).strip() if row.device_id else None,
        device_name=str(row.device_name).strip() if row.device_name else None,
        cwd=row.cwd,
        project=row.project,
        git_repo=row.git_repo,
        git_branch=row.git_branch,
        ended_at=row.ended_at,
        loop_mode=str(row.loop_mode or "assist"),
        permission_mode=str(row.permission_mode or "bypass"),
        primary_thread_id=thread_id,
    )


def live_control_capability_available(
    db: Session,
    *,
    session_id: UUID | str,
    capability: str,
) -> bool:
    """Return whether an attached live kernel connection grants a capability."""

    column = {
        "send": LiveSessionConnection.can_send_input,
        "interrupt": LiveSessionConnection.can_interrupt,
        "terminate": LiveSessionConnection.can_terminate,
    }.get(capability)
    if column is None:
        raise ValueError(f"unknown live control capability: {capability}")
    return (
        db.query(LiveSessionConnection.id)
        .join(LiveSessionRun, LiveSessionRun.id == LiveSessionConnection.run_id)
        .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
        .filter(
            LiveSessionThread.session_id == str(session_id),
            LiveSessionConnection.state == "attached",
            LiveSessionConnection.released_at.is_(None),
            column == 1,
        )
        .limit(1)
        .first()
        is not None
    )


def live_session_closed_for_input(db: Session, session: LiveControlSession) -> bool:
    if session.ended_at is not None:
        return True
    row = (
        db.query(LiveRuntimeState.terminal_state)
        .filter(LiveRuntimeState.session_id == session.id)
        .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
        .first()
    )
    if row is None:
        return False
    terminal_state = str(row[0] or "").strip()
    return terminal_state not in {"", "finished", "host_expired"}
