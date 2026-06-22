from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.models import Runner
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_kernel_projection import project_session_control_fields


def managed_runner_host_state(db: Session, session: AgentSession) -> str | None:
    """Return the current runner connection state for a managed session."""
    runner_id = project_session_control_fields(db, session).source_runner_id
    if runner_id is None:
        return None

    try:
        runner = db.query(Runner).filter(Runner.id == int(runner_id)).first()
    except SQLAlchemyError:
        return "unknown"
    if runner is None:
        return "unknown"

    owner_id = getattr(runner, "owner_id", None)
    if owner_id is not None and get_runner_connection_manager().is_online(int(owner_id), int(runner.id)):
        return "online"

    status = str(getattr(runner, "status", "") or "").strip().lower()
    if status == "online":
        return "stale"
    if status in {"offline", "revoked"}:
        return "offline"
    return "unknown"
