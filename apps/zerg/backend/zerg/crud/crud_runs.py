"""CRUD operations for Agent Runs."""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.models.models import ThreadMessage
from zerg.schemas.schemas import RunTrigger
from zerg.utils.time import utc_now_naive


def create_run(
    db: Session,
    *,
    agent_id: int,
    thread_id: int,
    trigger: str = "manual",
    status: str = "queued",
) -> AgentRun:
    """Insert a new *AgentRun* row.

    Minimal helper to keep service layers free from SQLAlchemy internals.
    """

    # Validate trigger and status enum values
    try:
        trigger_enum = RunTrigger(trigger)
    except ValueError:
        raise ValueError(f"Invalid run trigger: {trigger}")
    try:
        status_enum = RunStatus(status)
    except ValueError:
        raise ValueError(f"Invalid run status: {status}")
    run_row = AgentRun(
        agent_id=agent_id,
        thread_id=thread_id,
        trigger=trigger_enum,
        status=status_enum,
    )
    db.add(run_row)
    db.commit()
    db.refresh(run_row)
    return run_row


def mark_running(db: Session, run_id: int, *, started_at: Optional[datetime] = None) -> Optional[AgentRun]:
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        return None

    started_at = started_at or utc_now_naive()
    # Set to running status
    row.status = RunStatus.RUNNING
    row.started_at = started_at
    db.commit()
    db.refresh(row)
    return row


def mark_finished(
    db: Session,
    run_id: int,
    *,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    total_tokens: Optional[int] = None,
    total_cost_usd: Optional[float] = None,
    summary: Optional[str] = None,
) -> Optional[AgentRun]:
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        return None

    finished_at = finished_at or utc_now_naive()
    if row.started_at and duration_ms is None:
        duration_ms = int((finished_at - row.started_at).total_seconds() * 1000)

    # If no summary provided, extract from thread's first assistant message
    if summary is None and row.thread_id:
        summary = _extract_run_summary(db, row.thread_id)
        if summary:
            import logging

            logger = logging.getLogger(__name__)
            logger.info(f"Auto-extracted summary for run {run_id}: {summary[:100]}...")
        else:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"No summary extracted for run {run_id} (thread {row.thread_id})")

    # Set to success status
    row.status = RunStatus.SUCCESS
    row.finished_at = finished_at
    row.duration_ms = duration_ms
    row.total_tokens = total_tokens
    row.total_cost_usd = total_cost_usd
    row.summary = summary

    db.commit()
    db.refresh(row)
    return row


def _extract_run_summary(db: Session, thread_id: int, max_length: int = 500) -> str:
    """Extract summary from thread's first assistant message.

    Args:
        db: Database session
        thread_id: Thread ID to extract from
        max_length: Maximum summary length (default 500 chars)

    Returns:
        Summary text (truncated if needed) or empty string if no assistant messages
    """
    # Get first assistant message from thread
    first_assistant_msg = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .filter(ThreadMessage.role == "assistant")
        .order_by(ThreadMessage.id.asc())
        .first()
    )

    if not first_assistant_msg or not first_assistant_msg.content:
        return ""

    # Extract text content
    content = first_assistant_msg.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Handle array of content blocks (might be JSON)
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        text = " ".join(text_parts)
    elif isinstance(content, dict):
        # Handle single content block
        text = content.get("text", str(content))
    else:
        text = str(content)

    # Truncate if needed
    if len(text) > max_length:
        text = text[:max_length].strip() + "..."

    return text.strip()


def mark_failed(
    db: Session,
    run_id: int,
    *,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[AgentRun]:
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        return None

    finished_at = finished_at or utc_now_naive()
    if row.started_at and duration_ms is None:
        duration_ms = int((finished_at - row.started_at).total_seconds() * 1000)

    # Set to failed status
    row.status = RunStatus.FAILED
    row.finished_at = finished_at
    row.duration_ms = duration_ms
    row.error = error

    db.commit()
    db.refresh(row)
    return row


def list_runs(db: Session, agent_id: int, *, limit: int = 20, owner_id: Optional[int] = None):
    """Return the most recent runs for *agent_id* ordered DESC by id.

    If *owner_id* is provided, the agent must be owned by that user.
    """
    query = db.query(AgentRun).filter(AgentRun.agent_id == agent_id)
    if owner_id is not None:
        query = query.join(Agent, Agent.id == AgentRun.agent_id).filter(Agent.owner_id == owner_id)
    return query.order_by(AgentRun.id.desc()).limit(limit).all()
