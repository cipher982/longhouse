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
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.agents_def.zerg_react_agent import clear_evidence_mount_warning
from zerg.crud import crud
from zerg.managers.agent_runner import AgentInterrupted
from zerg.managers.agent_runner import AgentRunner
from zerg.models.enums import RunStatus
from zerg.models.enums import ThreadType
from zerg.models.models import Agent as AgentModel
from zerg.models.models import AgentRun
from zerg.models.models import Thread as ThreadModel
from zerg.models.models import WorkerJob
from zerg.prompts import build_supervisor_prompt
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
    status: str  # 'success' | 'failed' | 'cancelled' | 'deferred' | 'error'
    result: str | None = None
    error: str | None = None
    duration_ms: int = 0
    debug_url: str | None = None  # Dashboard deep link


class SupervisorService:
    """Service for managing supervisor agent execution."""

    # Bump this whenever BASE_SUPERVISOR_PROMPT meaningfully changes.
    SUPERVISOR_PROMPT_VERSION = 1

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
                # Keep the supervisor prompt and tool allowlist in sync with code.
                # Supervisor agents are system-managed; stale prompts routinely cause
                # "I searched but found nothing" hallucinations because the model is
                # running with outdated tool descriptions.
                changed = False
                user = crud.get_user(self.db, owner_id)
                if user:
                    desired_prompt = build_supervisor_prompt(user)
                    if agent.system_instructions != desired_prompt:
                        agent.system_instructions = desired_prompt
                        changed = True

                supervisor_tools = [
                    "spawn_worker",
                    "list_workers",
                    "read_worker_result",
                    "read_worker_file",
                    "grep_workers",
                    "get_worker_metadata",
                    "get_current_time",
                    "http_request",
                    "runner_list",
                    "runner_create_enroll_token",
                    "send_email",
                    "knowledge_search",
                    "web_search",
                    "web_fetch",
                    # Personal tools (Phase 4 v2.1)
                    "get_current_location",
                    "get_whoop_data",
                    "search_notes",
                ]
                if agent.allowed_tools != supervisor_tools:
                    agent.allowed_tools = supervisor_tools
                    changed = True

                # Track prompt version in config for future migrations/debugging.
                if config.get("prompt_version") != self.SUPERVISOR_PROMPT_VERSION:
                    config["prompt_version"] = self.SUPERVISOR_PROMPT_VERSION
                    agent.config = config
                    changed = True

                if changed:
                    self.db.commit()
                    self.db.refresh(agent)

                logger.debug(f"Found existing supervisor agent {agent.id} for user {owner_id}")
                return agent

        # Create new supervisor agent
        logger.info(f"Creating supervisor agent for user {owner_id}")

        # Fetch user for context-aware prompt composition
        user = crud.get_user(self.db, owner_id)
        if not user:
            raise ValueError(f"User {owner_id} not found")

        supervisor_config = {
            "is_supervisor": True,
            "prompt_version": self.SUPERVISOR_PROMPT_VERSION,
            "temperature": 0.7,
            "max_tokens": 2000,
            "reasoning_effort": "none",  # Disable reasoning for fast responses
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
            "runner_list",
            "runner_create_enroll_token",
            "send_email",
            # V1.1: knowledge base search for user context
            "knowledge_search",
            # V1.2: web research capabilities
            "web_search",
            "web_fetch",
            # V2.1 Phase 4: Personal tools (location, health, notes)
            "get_current_location",
            "get_whoop_data",
            "search_notes",
        ]

        agent = crud.create_agent(
            db=self.db,
            owner_id=owner_id,
            name="Supervisor",
            model=DEFAULT_MODEL_ID,
            system_instructions=build_supervisor_prompt(user),
            task_instructions="You are helping the user accomplish their goals. " "Analyze their request and decide how to handle it.",
            config=supervisor_config,
        )
        # Set allowed_tools (not supported in crud.create_agent)
        agent.allowed_tools = supervisor_tools
        self.db.commit()
        self.db.refresh(agent)

        logger.info(f"Created supervisor agent {agent.id} for user {owner_id}")
        return agent

    def get_or_create_supervisor_thread(self, owner_id: int, agent: AgentModel | None = None) -> ThreadModel:
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
                logger.debug(f"Found existing supervisor thread {thread.id} for user {owner_id}")
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
            elapsed_str = (
                f"{int(elapsed.total_seconds() / 60)}m ago" if elapsed.total_seconds() >= 60 else f"{int(elapsed.total_seconds())}s ago"
            )

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

        Strategy to handle both race conditions and back-to-back requests:
        1. Find all marked messages, sorted newest-first
        2. Keep the newest one ONLY if it's < min_age_seconds old (concurrent request protection)
        3. Delete all others (prevents accumulation from back-to-back requests)

        Args:
            thread_id: The thread to clean up
            min_age_seconds: Protect messages newer than this from deletion (default: 5s)

        Returns:
            Number of messages deleted.
        """
        from datetime import timedelta

        from zerg.models.models import ThreadMessage

        age_cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)

        # Find ALL marked messages, sorted by sent_at descending (newest first)
        all_marked = (
            self.db.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread_id,
                ThreadMessage.role == "system",
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .order_by(ThreadMessage.sent_at.desc())
            .all()
        )

        if not all_marked:
            return 0

        # Determine which messages to delete:
        # - Keep newest ONLY if it's fresh (< min_age_seconds) - protects concurrent requests
        # - Delete ALL others (prevents accumulation)
        messages_to_delete = []
        newest = all_marked[0]
        newest_sent_at = newest.sent_at
        if newest_sent_at.tzinfo is None:
            newest_sent_at = newest_sent_at.replace(tzinfo=timezone.utc)

        if newest_sent_at >= age_cutoff:
            # Newest is fresh - keep it, delete all others
            messages_to_delete = all_marked[1:]
        else:
            # Newest is stale - delete all (we're about to inject a new one)
            messages_to_delete = all_marked

        count = len(messages_to_delete)
        for msg in messages_to_delete:
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
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        return_on_deferred: bool = True,
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
            model_override: Optional model to use instead of agent's default
            reasoning_effort: Optional reasoning effort (none, low, medium, high)
            return_on_deferred: If True, return a DEFERRED response once the timeout hits.
                If False, emit SUPERVISOR_DEFERRED but continue running in the background until completion.

        Returns:
            SupervisorRunResult with run details and result
        """
        start_time = datetime.now(timezone.utc)
        started_at_naive = start_time.replace(tzinfo=None)

        # Get or create supervisor components
        agent = self.get_or_create_supervisor_agent(owner_id)

        # Always refresh supervisor prompt from current templates + user context.
        # The supervisor agent is long-lived; without this, prompt updates (and user profile changes)
        # won't take effect until the agent row is recreated.
        user = crud.get_user(self.db, owner_id)
        if not user:
            raise ValueError(f"User {owner_id} not found")
        agent.system_instructions = build_supervisor_prompt(user)
        self.db.commit()
        logger.debug(f"Refreshed supervisor prompt for agent {agent.id} (user {owner_id})")
        thread = self.get_or_create_supervisor_thread(owner_id, agent)

        # Use existing run or create new one
        if run_id:
            run = self.db.query(AgentRun).filter(AgentRun.id == run_id).first()
            if not run:
                raise ValueError(f"Run {run_id} not found")
            logger.info(f"Using existing supervisor run {run.id}", extra={"tag": "AGENT"})
        else:
            # Create run record (fallback for direct calls)
            from zerg.models.enums import RunTrigger

            run = AgentRun(
                agent_id=agent.id,
                thread_id=thread.id,
                status=RunStatus.RUNNING,
                trigger=RunTrigger.API,
                started_at=started_at_naive,
            )
            self.db.add(run)
            self.db.commit()
            self.db.refresh(run)
            logger.info(f"Created new supervisor run {run.id}", extra={"tag": "AGENT"})

        # Ensure started_at is populated for existing runs as well.
        if run.started_at is None:
            run.started_at = started_at_naive
            self.db.commit()

        logger.info(f"Starting supervisor run {run.id} for user {owner_id}, task: {task[:50]}...", extra={"tag": "AGENT"})

        # Generate unique message_id for this assistant response
        # This ID is stable across supervisor_started -> supervisor_token -> supervisor_complete
        message_id = str(uuid.uuid4())

        # Persist message_id to the run for continuation lookups
        run.assistant_message_id = message_id
        self.db.commit()

        # Check if this is a continuation run (processing worker result from a deferred run)
        # If so, include continuation_of_message_id so frontend creates a NEW message bubble
        # instead of overwriting the original "delegating to worker" message
        is_continuation = run.continuation_of_run_id is not None
        continuation_of_message_id = None
        if is_continuation:
            # Look up the original run's assistant_message_id for proper UUID compliance
            original_run = self.db.query(AgentRun).filter(AgentRun.id == run.continuation_of_run_id).first()
            if original_run and original_run.assistant_message_id:
                continuation_of_message_id = original_run.assistant_message_id
            else:
                # Fallback: generate a new UUID if original run's message_id is not available
                # This maintains schema compliance (UUID format) while still signaling continuation
                continuation_of_message_id = str(uuid.uuid4())
                logger.warning(
                    f"Original run {run.continuation_of_run_id} has no assistant_message_id, "
                    f"using generated UUID {continuation_of_message_id}"
                )

        # Emit supervisor started event
        from zerg.services.event_store import emit_run_event

        started_payload: dict = {
            "thread_id": thread.id,
            "task": task,
            "owner_id": owner_id,
            "message_id": message_id,
        }
        if continuation_of_message_id:
            started_payload["continuation_of_message_id"] = continuation_of_message_id

        await emit_run_event(
            db=self.db,
            run_id=run.id,
            event_type="supervisor_started",
            payload=started_payload,
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
            # Continuation tasks are internal orchestration messages - they should be
            # stored for LLM context but NOT shown to users in chat history
            crud.create_thread_message(
                db=self.db,
                thread_id=thread.id,
                role="user",
                content=task,
                processed=False,
                internal=is_continuation,  # Mark continuation prompts as internal
            )
            self.db.commit()

            # Emit thinking event
            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="supervisor_thinking",
                payload={
                    "message": "Analyzing your request...",
                    "owner_id": owner_id,
                },
            )

            # Set supervisor run context for spawn_worker correlation and tool event emission
            from zerg.services.supervisor_context import reset_supervisor_context
            from zerg.services.supervisor_context import set_supervisor_context

            _supervisor_ctx_tokens = set_supervisor_context(run_id=run.id, db=self.db, owner_id=owner_id, message_id=message_id)

            # Set up injected emitter for event emission (Phase 2 of emitter refactor)
            # SupervisorEmitter always emits supervisor_tool_* events regardless of contextvar state
            # Note: Emitter does NOT hold a DB session - event emission opens its own session
            from zerg.events import SupervisorEmitter
            from zerg.events import reset_emitter
            from zerg.events import set_emitter

            _supervisor_emitter = SupervisorEmitter(
                run_id=run.id,
                owner_id=owner_id,
                message_id=message_id,
            )
            _emitter_token = set_emitter(_supervisor_emitter)

            # Set user context for token streaming (required for real-time SSE tokens)
            from zerg.callbacks.token_stream import set_current_db_session
            from zerg.callbacks.token_stream import set_current_user_id

            _user_ctx_token = set_current_user_id(owner_id)
            _db_ctx_token = set_current_db_session(self.db)

            # Run the agent with timeout (shielded so timeout doesn't cancel work)
            runner = AgentRunner(agent, model_override=model_override, reasoning_effort=reasoning_effort)
            run_task = asyncio.create_task(runner.run_thread(self.db, thread))
            try:
                # asyncio.shield() prevents timeout from cancelling the task -
                # the timeout stops WAITING, not the WORK itself
                created_messages = await asyncio.wait_for(
                    asyncio.shield(run_task),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Timeout migration: run continues in background, we return deferred status
                # Calculate duration for the deferred event
                end_time = datetime.now(timezone.utc)
                duration_ms = int((end_time - start_time).total_seconds() * 1000)

                # Update run status to DEFERRED (not FAILED)
                run.status = RunStatus.DEFERRED
                run.duration_ms = duration_ms
                self.db.commit()

                # Emit deferred event (not error)
                await emit_run_event(
                    db=self.db,
                    run_id=run.id,
                    event_type="supervisor_deferred",
                    payload={
                        "agent_id": agent.id,
                        "thread_id": thread.id,
                        "message": "Still working on this in the background. I'll continue when ready.",
                        "timeout_seconds": timeout,
                        "attach_url": f"/api/jarvis/runs/{run.id}/stream",
                        "owner_id": owner_id,
                        "message_id": message_id,
                    },
                )

                # v2.2: Also emit RUN_UPDATED for dashboard visibility
                await emit_run_event(
                    db=self.db,
                    run_id=run.id,
                    event_type="run_updated",
                    payload={
                        "agent_id": agent.id,
                        "status": "deferred",
                        "thread_id": thread.id,
                        "owner_id": owner_id,
                    },
                )

                logger.info(f"Supervisor run {run.id} deferred after {timeout}s timeout (continuing in background until completion)")

                if return_on_deferred:
                    # Return deferred result - NOT an error.
                    # Note: In the production HTTP flows, supervisor runs are executed in a long-lived
                    # background task (see jarvis_supervisor/jarvis_chat) and can pass
                    # return_on_deferred=False to keep the DB session alive until completion.
                    return SupervisorRunResult(
                        run_id=run.id,
                        thread_id=thread.id,
                        status="deferred",
                        result="Still working on this in the background. I'll let you know when it's done.",
                        duration_ms=duration_ms,
                        debug_url=f"/supervisor/{run.id}",
                    )

                # Background mode: keep awaiting the original run_task to completion, then mark the run
                # finished and persist the result (SSE streams can close on SUPERVISOR_DEFERRED).
                created_messages = await run_task

            except AgentInterrupted as interrupt:
                # LangGraph interrupt (spawn_worker waiting for worker completion)
                # The graph state is checkpointed - we'll resume when worker completes
                end_time = datetime.now(timezone.utc)
                duration_ms = int((end_time - start_time).total_seconds() * 1000)

                # Update run status to WAITING
                run.status = RunStatus.WAITING
                run.duration_ms = duration_ms
                self.db.commit()

                # Extract interrupt payload
                interrupt_value = interrupt.interrupt_value
                job_id = interrupt_value.get("job_id") if isinstance(interrupt_value, dict) else None
                interrupt_message = (
                    interrupt_value.get("message", "Working on this in the background...")
                    if isinstance(interrupt_value, dict)
                    else str(interrupt_value)
                )

                # Emit waiting event (similar to deferred but semantically different)
                await emit_run_event(
                    db=self.db,
                    run_id=run.id,
                    event_type="supervisor_waiting",
                    payload={
                        "agent_id": agent.id,
                        "thread_id": thread.id,
                        "job_id": job_id,
                        "message": interrupt_message,
                        "owner_id": owner_id,
                        "message_id": message_id,
                        "close_stream": False,  # Keep SSE open for resume
                    },
                )

                await emit_run_event(
                    db=self.db,
                    run_id=run.id,
                    event_type="run_updated",
                    payload={
                        "agent_id": agent.id,
                        "status": "waiting",
                        "thread_id": thread.id,
                        "owner_id": owner_id,
                    },
                )

                logger.info(f"Supervisor run {run.id} interrupted (WAITING for worker job {job_id})")

                return SupervisorRunResult(
                    run_id=run.id,
                    thread_id=thread.id,
                    status="waiting",
                    result=interrupt_message,
                    duration_ms=duration_ms,
                    debug_url=f"/supervisor/{run.id}",
                )

            finally:
                # Always reset context and emitter even on timeout/deferred
                reset_supervisor_context(_supervisor_ctx_tokens)
                reset_emitter(_emitter_token)
                # Reset user context
                from zerg.callbacks.token_stream import current_db_session_var
                from zerg.callbacks.token_stream import current_user_id_var

                current_user_id_var.reset(_user_ctx_token)
                current_db_session_var.reset(_db_ctx_token)

            # Extract final result (last assistant message)
            result_text = None
            for msg in reversed(created_messages):
                if msg.role == "assistant" and msg.content:
                    result_text = msg.content
                    break

            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # NOTE: Old "durable runs" code that checked for worker spawns after completion
            # has been removed. With the interrupt/resume pattern, spawn_worker calls interrupt()
            # which raises AgentInterrupted before we get here. See the AgentInterrupted handler above.

            # Update run status
            run.status = RunStatus.SUCCESS
            run.finished_at = end_time.replace(tzinfo=None)
            run.duration_ms = duration_ms
            if runner.usage_total_tokens:
                run.total_tokens = runner.usage_total_tokens
            self.db.commit()

            # Emit completion event with SupervisorResult-aligned schema
            # Note: summary/recommendations/caveats would require parsing agent response
            # For now, include required fields and let frontend extract details
            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="supervisor_complete",
                payload={
                    "agent_id": agent.id,
                    "thread_id": thread.id,
                    "result": result_text or "(No result)",
                    "status": "success",
                    "duration_ms": duration_ms,
                    "debug_url": f"/supervisor/{run.id}",
                    "owner_id": owner_id,
                    "message_id": message_id,
                    # Token usage for debug/power mode
                    "usage": {
                        "prompt_tokens": runner.usage_prompt_tokens,
                        "completion_tokens": runner.usage_completion_tokens,
                        "total_tokens": runner.usage_total_tokens,
                        "reasoning_tokens": runner.usage_reasoning_tokens,
                    },
                },
            )

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="run_updated",
                payload={
                    "agent_id": agent.id,
                    "status": "success",
                    "finished_at": end_time.isoformat(),
                    "duration_ms": duration_ms,
                    "thread_id": thread.id,
                    "owner_id": owner_id,
                },
            )
            reset_seq(run.id)
            clear_evidence_mount_warning(run.id)

            logger.info(f"Supervisor run {run.id} completed in {duration_ms}ms", extra={"tag": "AGENT"})

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
                run.finished_at = end_time.replace(tzinfo=None)
                run.duration_ms = duration_ms
                self.db.commit()

            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="supervisor_complete",
                payload={
                    "agent_id": agent.id,
                    "thread_id": thread.id,
                    "status": "cancelled",
                    "duration_ms": duration_ms,
                    "owner_id": owner_id,
                },
            )

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="run_updated",
                payload={
                    "agent_id": agent.id,
                    "status": "cancelled",
                    "finished_at": end_time.isoformat(),
                    "duration_ms": duration_ms,
                    "thread_id": thread.id,
                    "owner_id": owner_id,
                },
            )

        except Exception as e:
            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update run status
            run.status = RunStatus.FAILED
            run.finished_at = end_time.replace(tzinfo=None)
            run.duration_ms = duration_ms
            run.error = str(e)
            self.db.commit()

            # Emit error event with consistent schema
            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="error",
                payload={
                    "agent_id": agent.id,
                    "thread_id": thread.id,
                    "message": str(e),
                    "status": "error",
                    "debug_url": f"/supervisor/{run.id}",
                    "owner_id": owner_id,
                },
            )

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="run_updated",
                payload={
                    "agent_id": agent.id,
                    "status": "failed",
                    "finished_at": end_time.isoformat(),
                    "duration_ms": duration_ms,
                    "error": str(e),
                    "thread_id": thread.id,
                    "owner_id": owner_id,
                },
            )
            reset_seq(run.id)
            clear_evidence_mount_warning(run.id)

            logger.exception(f"Supervisor run {run.id} failed: {e}")

            return SupervisorRunResult(
                run_id=run.id,
                thread_id=thread.id,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
                debug_url=f"/supervisor/{run.id}",
            )

    # NOTE: run_continuation() removed - replaced by LangGraph interrupt/resume pattern
    # See worker_resume.py for the new implementation using Command(resume=...)


__all__ = ["SupervisorService", "SupervisorRunResult", "SUPERVISOR_THREAD_TYPE"]
