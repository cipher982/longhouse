"""Supervisor Service - manages the "one brain per user" supervisor lifecycle.

This service handles:
- Finding or creating the user's long-lived supervisor thread
- Running the supervisor agent with streaming events
- Coordinating worker execution and result synthesis

The key invariant is ONE supervisor thread per user that persists across sessions.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any, AsyncIterator

from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.events import EventType, event_bus
from zerg.managers.agent_runner import AgentRunner
from zerg.models.enums import AgentStatus, RunStatus, ThreadType
from zerg.models.models import Agent as AgentModel
from zerg.models.models import AgentRun
from zerg.models.models import Thread as ThreadModel
from zerg.models.models import WorkerJob
from zerg.prompts.supervisor_prompt import get_supervisor_prompt
from zerg.services.supervisor_context import reset_seq
from zerg.services.thread_service import ThreadService
from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)

# Thread type for supervisor threads - distinguishes from regular agent threads
SUPERVISOR_THREAD_TYPE = ThreadType.SUPER

# Configuration for recent worker history injection
RECENT_WORKER_HISTORY_LIMIT = 5  # Max workers to show
RECENT_WORKER_HISTORY_MINUTES = 10  # Only show workers from last N minutes
# Marker to identify ephemeral context messages (for cleanup)
RECENT_WORKER_CONTEXT_MARKER = "<!-- RECENT_WORKER_CONTEXT -->"


@dataclass
class SupervisorRunResult:
    """Result from a supervisor run.

    Aligns with UI spec's SupervisorResult schema for frontend consumption.
    """

    run_id: int
    thread_id: int
    status: str  # 'success' | 'failed' | 'cancelled' | 'error'
    result: str | None = None
    error: str | None = None
    duration_ms: int = 0
    debug_url: str | None = None  # Dashboard deep link


class SupervisorService:
    """Service for managing supervisor agent execution."""

    def __init__(self, db: Session):
        """Initialize the supervisor service.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def get_or_create_supervisor_agent(self, owner_id: int) -> AgentModel:
        """Get or create the supervisor agent for a user.

        The supervisor agent is a special agent with supervisor tools enabled.
        Each user has exactly one supervisor agent.

        Args:
            owner_id: User ID

        Returns:
            The supervisor agent
        """
        from zerg.models_config import DEFAULT_MODEL_ID

        # Look for existing supervisor agent
        agents = crud.get_agents(self.db, owner_id=owner_id)
        for agent in agents:
            config = agent.config or {}
            if config.get("is_supervisor"):
                logger.debug(f"Found existing supervisor agent {agent.id} for user {owner_id}")
                return agent

        # Create new supervisor agent
        logger.info(f"Creating supervisor agent for user {owner_id}")

        supervisor_config = {
            "is_supervisor": True,
            "temperature": 0.7,
            "max_tokens": 2000,
        }

        supervisor_tools = [
            "spawn_worker",
            "list_workers",
            "read_worker_result",
            "read_worker_file",
            "grep_workers",
            "get_worker_metadata",
            "get_current_time",
            "http_request",
            "send_email",
        ]

        agent = crud.create_agent(
            db=self.db,
            owner_id=owner_id,
            name="Supervisor",
            model=DEFAULT_MODEL_ID,
            system_instructions=get_supervisor_prompt(),
            task_instructions="You are helping the user accomplish their goals. "
            "Analyze their request and decide how to handle it.",
            config=supervisor_config,
        )
        # Set allowed_tools (not supported in crud.create_agent)
        agent.allowed_tools = supervisor_tools
        self.db.commit()
        self.db.refresh(agent)

        logger.info(f"Created supervisor agent {agent.id} for user {owner_id}")
        return agent

    def get_or_create_supervisor_thread(
        self, owner_id: int, agent: AgentModel | None = None
    ) -> ThreadModel:
        """Get or create the long-lived supervisor thread for a user.

        Each user has exactly ONE supervisor thread that persists across sessions.
        This implements the "one brain" pattern where context accumulates.

        Args:
            owner_id: User ID
            agent: Optional supervisor agent (will be created if not provided)

        Returns:
            The supervisor thread
        """
        if agent is None:
            agent = self.get_or_create_supervisor_agent(owner_id)

        # Look for existing supervisor thread
        threads = crud.get_threads(self.db, agent_id=agent.id)
        for thread in threads:
            if thread.thread_type == SUPERVISOR_THREAD_TYPE:
                logger.debug(
                    f"Found existing supervisor thread {thread.id} for user {owner_id}"
                )
                return thread

        # Create new supervisor thread
        logger.info(f"Creating supervisor thread for user {owner_id}")

        thread = ThreadService.create_thread_with_system_message(
            self.db,
            agent,
            title="Supervisor",
            thread_type=SUPERVISOR_THREAD_TYPE,
            active=True,
        )
        self.db.commit()

        logger.info(f"Created supervisor thread {thread.id} for user {owner_id}")
        return thread

    def _build_recent_worker_context(self, owner_id: int) -> str | None:
        """Build context message with recent worker history.

        v2.0 Improvement: Auto-inject recent worker results so the supervisor
        doesn't have to call list_workers to check for duplicate work.

        The message includes a marker for cleanup - see _cleanup_stale_worker_context().

        Returns:
            Context string if there are recent workers, None otherwise.
        """
        from datetime import timedelta

        # Query recent workers
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=RECENT_WORKER_HISTORY_MINUTES)
        recent_jobs = (
            self.db.query(WorkerJob)
            .filter(
                WorkerJob.owner_id == owner_id,
                WorkerJob.created_at >= cutoff,
                WorkerJob.status.in_(["success", "failed", "running"]),
            )
            .order_by(WorkerJob.created_at.desc())
            .limit(RECENT_WORKER_HISTORY_LIMIT)
            .all()
        )

        if not recent_jobs:
            return None

        # Try to get artifact store for richer summaries, but don't fail if unavailable
        artifact_store = None
        try:
            artifact_store = WorkerArtifactStore()
        except (OSError, PermissionError) as e:
            logger.warning(f"WorkerArtifactStore unavailable, using task summaries only: {e}")

        # Build context with marker for cleanup
        lines = [
            RECENT_WORKER_CONTEXT_MARKER,  # Marker for identifying ephemeral context
            "## Recent Worker Activity (last 10 minutes)",
            "Check if any of these results already answer the user's question before spawning new workers:\n",
        ]

        for job in recent_jobs:
            # Calculate elapsed time (handle naive vs aware datetimes)
            job_created = job.created_at
            if job_created.tzinfo is None:
                job_created = job_created.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - job_created
            elapsed_str = f"{int(elapsed.total_seconds() / 60)}m ago" if elapsed.total_seconds() >= 60 else f"{int(elapsed.total_seconds())}s ago"

            # Get summary from artifact store if available
            summary = None
            if artifact_store and job.worker_id and job.status in ["success", "failed"]:
                try:
                    metadata = artifact_store.get_worker_metadata(job.worker_id)
                    summary = metadata.get("summary")
                except Exception:
                    pass

            if not summary:
                # Truncate task as fallback
                summary = job.task[:100] + "..." if len(job.task) > 100 else job.task

            status_emoji = {"success": "✓", "failed": "✗", "running": "⋯"}.get(job.status, "?")
            lines.append(f"- Job {job.id} [{status_emoji} {job.status.upper()}] ({elapsed_str})")
            lines.append(f"  {summary}\n")

        lines.append("Use read_worker_result(job_id) to get full details from any of these.")

        return "\n".join(lines)

    def _cleanup_stale_worker_context(self, thread_id: int, min_age_seconds: float = 5.0) -> int:
        """Delete previous recent worker context messages from the thread.

        This prevents stale context from accumulating across runs.
        Messages are identified by the RECENT_WORKER_CONTEXT_MARKER.

        To avoid race conditions with concurrent requests, only deletes messages
        older than min_age_seconds. This ensures we don't delete context that
        was just injected by a concurrent request.

        Args:
            thread_id: The thread to clean up
            min_age_seconds: Only delete messages older than this (default: 5s)

        Returns:
            Number of messages deleted.
        """
        from datetime import timedelta
        from zerg.models.models import ThreadMessage

        # Only delete messages older than min_age_seconds to avoid race conditions
        # with concurrent requests
        age_cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)

        # Find and delete messages containing the marker that are old enough
        stale_messages = (
            self.db.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread_id,
                ThreadMessage.role == "system",
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
                ThreadMessage.created_at < age_cutoff,
            )
            .all()
        )

        count = len(stale_messages)
        for msg in stale_messages:
            self.db.delete(msg)

        if count > 0:
            logger.debug(f"Cleaned up {count} stale worker context message(s) from thread {thread_id}")

        return count

    async def run_supervisor(
        self,
        owner_id: int,
        task: str,
        run_id: int | None = None,
        timeout: int = 60,
    ) -> SupervisorRunResult:
        """Run the supervisor agent with a task.

        This method:
        1. Gets or creates the supervisor thread for the user
        2. Uses existing run record OR creates a new one
        3. Adds the task as a user message
        4. Runs the supervisor agent
        5. Returns the result

        Args:
            owner_id: User ID
            task: The task/question from the user
            run_id: Optional existing run ID (avoids duplicate run creation)
            timeout: Maximum execution time in seconds

        Returns:
            SupervisorRunResult with run details and result
        """
        start_time = datetime.now(timezone.utc)

        # Get or create supervisor components
        agent = self.get_or_create_supervisor_agent(owner_id)
        thread = self.get_or_create_supervisor_thread(owner_id, agent)

        # Use existing run or create new one
        if run_id:
            run = self.db.query(AgentRun).filter(AgentRun.id == run_id).first()
            if not run:
                raise ValueError(f"Run {run_id} not found")
            logger.info(f"Using existing supervisor run {run.id}")
        else:
            # Create run record (fallback for direct calls)
            from zerg.models.enums import RunTrigger
            run = AgentRun(
                agent_id=agent.id,
                thread_id=thread.id,
                status=RunStatus.RUNNING,
                trigger=RunTrigger.API,
            )
            self.db.add(run)
            self.db.commit()
            self.db.refresh(run)
            logger.info(f"Created new supervisor run {run.id}")

        logger.info(
            f"Starting supervisor run {run.id} for user {owner_id}, task: {task[:50]}..."
        )

        # Emit supervisor started event
        await event_bus.publish(
            EventType.SUPERVISOR_STARTED,
            {
                "event_type": EventType.SUPERVISOR_STARTED,
                "run_id": run.id,
                "thread_id": thread.id,
                "task": task,
                "owner_id": owner_id,
            },
        )

        try:
            # v2.0: Inject recent worker history context before user message
            # This prevents redundant worker spawns by showing the supervisor
            # what work has been done recently
            #
            # IMPORTANT: Clean up any stale context messages first to prevent
            # accumulation of outdated "X minutes ago" timestamps
            self._cleanup_stale_worker_context(thread.id)

            recent_worker_context = self._build_recent_worker_context(owner_id)
            if recent_worker_context:
                logger.debug(f"Injecting recent worker context for user {owner_id}")
                crud.create_thread_message(
                    db=self.db,
                    thread_id=thread.id,
                    role="system",
                    content=recent_worker_context,
                    processed=True,  # Mark as processed so agent doesn't re-process
                )

            # Add task as user message
            crud.create_thread_message(
                db=self.db,
                thread_id=thread.id,
                role="user",
                content=task,
                processed=False,
            )
            self.db.commit()

            # Emit thinking event
            await event_bus.publish(
                EventType.SUPERVISOR_THINKING,
                {
                    "event_type": EventType.SUPERVISOR_THINKING,
                    "run_id": run.id,
                    "message": "Analyzing your request...",
                    "owner_id": owner_id,
                },
            )

            # Set supervisor run context so spawn_worker can correlate workers
            from zerg.services.supervisor_context import (
                set_supervisor_run_id,
                reset_supervisor_run_id,
            )
            _supervisor_ctx_token = set_supervisor_run_id(run.id)

            # Run the agent with timeout
            runner = AgentRunner(agent)
            try:
                created_messages = await asyncio.wait_for(
                    runner.run_thread(self.db, thread),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Supervisor execution timed out after {timeout}s")
            finally:
                # Always reset context even on timeout
                reset_supervisor_run_id(_supervisor_ctx_token)

            # Extract final result (last assistant message)
            result_text = None
            for msg in reversed(created_messages):
                if msg.role == "assistant" and msg.content:
                    result_text = msg.content
                    break

            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update run status
            run.status = RunStatus.SUCCESS
            run.finished_at = end_time
            if runner.usage_total_tokens:
                run.total_tokens = runner.usage_total_tokens
            self.db.commit()

            # Emit completion event with SupervisorResult-aligned schema
            # Note: summary/recommendations/caveats would require parsing agent response
            # For now, include required fields and let frontend extract details
            await event_bus.publish(
                EventType.SUPERVISOR_COMPLETE,
                {
                    "event_type": EventType.SUPERVISOR_COMPLETE,
                    "run_id": run.id,
                    "thread_id": thread.id,
                    "result": result_text or "(No result)",
                    "status": "success",
                    "duration_ms": duration_ms,
                    "debug_url": f"/supervisor/{run.id}",
                    "owner_id": owner_id,
                },
            )
            reset_seq(run.id)

            logger.info(f"Supervisor run {run.id} completed in {duration_ms}ms")

            return SupervisorRunResult(
                run_id=run.id,
                thread_id=thread.id,
                status="success",
                result=result_text,
                duration_ms=duration_ms,
                debug_url=f"/supervisor/{run.id}",
            )

        except asyncio.CancelledError:
            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update run status to cancelled if not already terminal
            if run.status not in {RunStatus.CANCELLED, RunStatus.SUCCESS, RunStatus.FAILED}:
                run.status = RunStatus.CANCELLED
                run.finished_at = end_time
                self.db.commit()

            await event_bus.publish(
                EventType.SUPERVISOR_COMPLETE,
                {
                    "event_type": EventType.SUPERVISOR_COMPLETE,
                    "run_id": run.id,
                    "thread_id": thread.id,
                    "status": "cancelled",
                    "message": "Supervisor run cancelled",
                    "duration_ms": duration_ms,
                    "debug_url": f"/supervisor/{run.id}",
                    "owner_id": owner_id,
                },
            )
            reset_seq(run.id)

            logger.info(f"Supervisor run {run.id} cancelled after {duration_ms}ms")

            return SupervisorRunResult(
                run_id=run.id,
                thread_id=thread.id,
                status="cancelled",
                result=None,
                duration_ms=duration_ms,
                debug_url=f"/supervisor/{run.id}",
            )

        except Exception as e:
            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update run status
            run.status = RunStatus.FAILED
            run.finished_at = end_time
            run.error = str(e)
            self.db.commit()

            # Emit error event with consistent schema
            await event_bus.publish(
                EventType.ERROR,
                {
                    "event_type": EventType.ERROR,
                    "run_id": run.id,
                    "thread_id": thread.id,
                    "message": str(e),
                    "status": "error",
                    "debug_url": f"/supervisor/{run.id}",
                    "owner_id": owner_id,
                },
            )
            reset_seq(run.id)

            logger.exception(f"Supervisor run {run.id} failed: {e}")

            return SupervisorRunResult(
                run_id=run.id,
                thread_id=thread.id,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
                debug_url=f"/supervisor/{run.id}",
            )


__all__ = ["SupervisorService", "SupervisorRunResult", "SUPERVISOR_THREAD_TYPE"]
