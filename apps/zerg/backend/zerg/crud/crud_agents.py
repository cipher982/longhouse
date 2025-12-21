"""CRUD operations for Agents."""

from datetime import datetime
from typing import Any
from typing import Dict
from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from zerg.models import Agent
from zerg.models import AgentMessage
from zerg.models import AgentRun
from zerg.models import Thread
from zerg.models import ThreadMessage
from zerg.models import Trigger
from zerg.models import WorkerJob
from zerg.utils.time import utc_now_naive


def _validate_cron_or_raise(expr: Optional[str]):
    """Raise ``ValueError`` if *expr* is not a valid crontab string."""

    if expr is None:
        return

    try:
        CronTrigger.from_crontab(expr)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid cron expression: {expr} ({exc})") from exc


def get_agents(
    db: Session,
    *,
    skip: int = 0,
    limit: int = 100,
    owner_id: Optional[int] = None,
):
    """Return a list of agents.

    If *owner_id* is provided the result is limited to agents owned by that
    user.  Otherwise all agents are returned (paginated).
    """

    # Eager-load relationships that the Pydantic ``Agent`` response model
    # serialises (``owner`` and ``messages``) so that FastAPI's response
    # rendering still works *after* the request-scoped SQLAlchemy Session is
    # closed.  Without this the lazy relationship access attempts to perform a
    # new query on a detached instance which raises ``DetachedInstanceError``
    # and bubbles up as a ``ResponseValidationError``.

    # Always use selectinload to avoid detached instance errors
    query = db.query(Agent).options(
        selectinload(Agent.owner),
        selectinload(Agent.messages),
    )
    if owner_id is not None:
        query = query.filter(Agent.owner_id == owner_id)

    return query.offset(skip).limit(limit).all()


def get_agent(db: Session, agent_id: int):
    """Get a single agent by ID"""
    return (
        db.query(Agent)
        .options(
            selectinload(Agent.owner),
            selectinload(Agent.messages),
        )
        .filter(Agent.id == agent_id)
        .first()
    )


def create_agent(
    db: Session,
    *,
    owner_id: int,
    name: Optional[str] = None,
    system_instructions: str,
    task_instructions: str,
    model: str,
    schedule: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """Create a new agent.

    ``owner_id`` is **required** – every agent belongs to exactly one user.
    ``name`` defaults to "New Agent" if not provided.
    """

    # Validate cron expression if provided
    _validate_cron_or_raise(schedule)

    # Create agent
    db_agent = Agent(
        owner_id=owner_id,
        name=name or "New Agent",
        system_instructions=system_instructions,
        task_instructions=task_instructions,
        model=model,
        status="idle",
        schedule=schedule,
        config=config,
        next_run_at=None,
        last_run_at=None,
    )
    db.add(db_agent)
    db.commit()
    db.refresh(db_agent)

    # Force load relationships to avoid detached instance errors
    _ = db_agent.owner
    _ = db_agent.messages

    return db_agent


def update_agent(
    db: Session,
    agent_id: int,
    name: Optional[str] = None,
    system_instructions: Optional[str] = None,
    task_instructions: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    schedule: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    allowed_tools: Optional[list] = None,
    next_run_at: Optional[datetime] = None,
    last_run_at: Optional[datetime] = None,
    last_error: Optional[str] = None,
):
    """Update an existing agent"""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if db_agent is None:
        return None

    # Update provided fields
    if name is not None:
        db_agent.name = name
    if system_instructions is not None:
        db_agent.system_instructions = system_instructions
    if task_instructions is not None:
        db_agent.task_instructions = task_instructions
    if model is not None:
        db_agent.model = model
    if status is not None:
        db_agent.status = status
    if schedule is not None:
        _validate_cron_or_raise(schedule)
        db_agent.schedule = schedule
    if config is not None:
        db_agent.config = config
    if allowed_tools is not None:
        db_agent.allowed_tools = allowed_tools
    if next_run_at is not None:
        db_agent.next_run_at = next_run_at
    if last_run_at is not None:
        db_agent.last_run_at = last_run_at
    if last_error is not None:
        db_agent.last_error = last_error

    db_agent.updated_at = utc_now_naive()
    db.commit()
    db.refresh(db_agent)
    return db_agent


def delete_agent(db: Session, agent_id: int):
    """Delete an agent and all dependent rows.

    NOTE: In production (Postgres), an Agent can be referenced by:
    - agent_threads / thread_messages
    - agent_runs (and worker_jobs.supervisor_run_id)
    - agent_messages (legacy)
    - triggers
    Deleting the Agent row directly can violate FK constraints, especially for
    temporary worker agents that create threads/messages during execution.
    """
    exists = db.query(Agent.id).filter(Agent.id == agent_id).first()
    if exists is None:
        return False

    # Triggers are linked via backref and do not cascade by default.
    db.query(Trigger).filter(Trigger.agent_id == agent_id).delete(synchronize_session=False)

    # Runs must be deleted before threads (AgentRun.thread_id FK).
    run_ids = [row[0] for row in db.query(AgentRun.id).filter(AgentRun.agent_id == agent_id).all()]
    if run_ids:
        # Worker jobs may reference supervisor runs; preserve jobs but remove correlation.
        db.query(WorkerJob).filter(WorkerJob.supervisor_run_id.in_(run_ids)).update(
            {WorkerJob.supervisor_run_id: None},
            synchronize_session="fetch",
        )
        db.query(AgentRun).filter(AgentRun.id.in_(run_ids)).delete(synchronize_session=False)

    # Delete thread messages + threads for this agent.
    thread_ids = [row[0] for row in db.query(Thread.id).filter(Thread.agent_id == agent_id).all()]
    if thread_ids:
        db.query(ThreadMessage).filter(ThreadMessage.thread_id.in_(thread_ids)).delete(synchronize_session=False)
        db.query(Thread).filter(Thread.id.in_(thread_ids)).delete(synchronize_session=False)

    # Legacy agent_messages table.
    db.query(AgentMessage).filter(AgentMessage.agent_id == agent_id).delete(synchronize_session=False)

    # Finally delete the agent itself.
    db.query(Agent).filter(Agent.id == agent_id).delete(synchronize_session=False)
    db.commit()
    return True


def get_agent_messages(db: Session, agent_id: int, skip: int = 0, limit: int = 100):
    """Get all messages for a specific agent"""
    return db.query(AgentMessage).filter(AgentMessage.agent_id == agent_id).order_by(AgentMessage.timestamp).offset(skip).limit(limit).all()


def create_agent_message(db: Session, agent_id: int, role: str, content: str):
    """Create a new message for an agent"""
    db_message = AgentMessage(agent_id=agent_id, role=role, content=content)
    db.add(db_message)
    db.commit()
    db.refresh(db_message)
    return db_message
