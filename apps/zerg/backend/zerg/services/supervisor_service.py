"""Supervisor Service - manages the "one brain per user" supervisor lifecycle.

This service handles:
- Finding or creating the user's long-lived supervisor thread
- Running the supervisor agent with streaming events
- Coordinating worker execution and result synthesis

The key invariant is ONE supervisor thread per user that persists across sessions.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import Session

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
    SUPERVISOR_PROMPT_VERSION = 2

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
                    "get_worker_evidence",
                    "get_tool_output",
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
            "get_worker_evidence",
            "get_tool_output",
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
        message_id: str | None = None,
        trace_id: str | None = None,
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
            message_id: Client-generated message ID (UUID). If None, one will be generated.
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
                model=model_override or agent.model,
                reasoning_effort=reasoning_effort,
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

        # Use client-provided message_id or generate one (for direct API calls / tests)
        # This ID is stable across supervisor_started -> supervisor_token -> supervisor_complete
        if message_id is None:
            message_id = str(uuid.uuid4())

        # Persist message_id to the run if not already set (jarvis_chat.py sets it on AgentRun creation)
        if run.assistant_message_id != message_id:
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

        # Resolve trace_id early for downstream event payloads and context
        effective_trace_id = trace_id or (str(run.trace_id) if run.trace_id else None)
        if not effective_trace_id:
            effective_trace_id = str(uuid.uuid4())
            # Persist to run for consistency
            run.trace_id = uuid.UUID(effective_trace_id)
            self.db.commit()

        started_payload: dict = {
            "thread_id": thread.id,
            "task": task,
            "owner_id": owner_id,
            "message_id": message_id,
            "trace_id": effective_trace_id,
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
                    "trace_id": effective_trace_id,
                },
            )

            # Set supervisor run context for spawn_worker correlation and tool event emission
            from zerg.services.supervisor_context import reset_supervisor_context
            from zerg.services.supervisor_context import set_supervisor_context

            _supervisor_ctx_token = set_supervisor_context(
                run_id=run.id,
                owner_id=owner_id,
                message_id=message_id,
                trace_id=effective_trace_id,
                model=model_override or agent.model,
                reasoning_effort=reasoning_effort,
            )

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
                trace_id=effective_trace_id,
            )
            _emitter_token = set_emitter(_supervisor_emitter)

            # Set user context for token streaming (required for real-time SSE tokens)
            from zerg.callbacks.token_stream import set_current_user_id

            _user_ctx_token = set_current_user_id(owner_id)

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
                        "trace_id": effective_trace_id,
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
                # Supervisor interrupt (spawn_worker waiting for worker completion)
                # Run state is persisted; we'll resume via AgentRunner.run_continuation
                end_time = datetime.now(timezone.utc)
                duration_ms = int((end_time - start_time).total_seconds() * 1000)

                # Update run status to WAITING (NOT committed yet - atomic with barrier creation)
                run.status = RunStatus.WAITING
                run.duration_ms = duration_ms
                # Persist partial token usage before WAITING (will be added to on resume)
                if runner.usage_total_tokens is not None:
                    run.total_tokens = runner.usage_total_tokens
                # NOTE: DO NOT commit here - we need WAITING + barrier to be atomic

                # Extract interrupt payload
                interrupt_value = interrupt.interrupt_value

                # TWO-PHASE COMMIT: Handle parallel workers (barrier pattern)
                # CRITICAL: Barrier creation and WAITING status must be in SAME transaction
                # to prevent race where worker completes before barrier exists
                if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "workers_pending":
                    from zerg.models.worker_barrier import BarrierJob
                    from zerg.models.worker_barrier import WorkerBarrier

                    job_ids = interrupt_value.get("job_ids", [])
                    created_jobs = interrupt_value.get("created_jobs", [])

                    logger.info(f"TWO-PHASE COMMIT: Creating barrier for {len(job_ids)} workers")

                    # PHASE 2: Create barrier FIRST (jobs are already created with status='created')
                    barrier = WorkerBarrier(
                        run_id=run.id,
                        expected_count=len(job_ids),
                        status="waiting",
                        # Set deadline 10 minutes from now (configurable)
                        deadline_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
                    )
                    self.db.add(barrier)
                    self.db.flush()  # Get barrier.id

                    # Create BarrierJob records with tool_call_id mapping
                    for job_info in created_jobs:
                        job = job_info["job"]
                        tool_call_id = job_info["tool_call_id"]
                        self.db.add(
                            BarrierJob(
                                barrier_id=barrier.id,
                                job_id=job.id,
                                tool_call_id=tool_call_id,
                                status="queued",  # Ready for pickup
                            )
                        )

                    # PHASE 3: Flip jobs from 'created' to 'queued' (workers can now pick them up)
                    for job_id in job_ids:
                        self.db.query(WorkerJob).filter(
                            WorkerJob.id == job_id,
                            WorkerJob.status == "created",
                        ).update({"status": "queued"})

                    self.db.commit()
                    logger.info(f"TWO-PHASE COMMIT complete: barrier={barrier.id}, {len(job_ids)} jobs queued")

                    # Emit worker_spawned events for UI (job_id → tool_call_id mapping)
                    # Must emit AFTER jobs are queued (UI expects valid jobs)
                    from zerg.services.event_store import append_run_event

                    for job_info in created_jobs:
                        job = job_info["job"]
                        tool_call_id = job_info["tool_call_id"]
                        task = job_info.get("task", job.task[:100] if job.task else "")
                        await append_run_event(
                            run_id=run.id,
                            event_type="worker_spawned",
                            payload={
                                "job_id": job.id,
                                "tool_call_id": tool_call_id,
                                "task": task,
                                "model": job.model,
                                "owner_id": owner_id,
                                "trace_id": effective_trace_id,
                            },
                        )
                    logger.info(f"Emitted {len(created_jobs)} worker_spawned events")

                    # For backwards compatibility with event payload
                    job_id = job_ids[0] if job_ids else None
                    interrupt_message = f"Working on {len(job_ids)} tasks in the background..."

                    # Check if any workers already completed (race safety)
                    already_completed = 0
                    for jid in job_ids:
                        worker_job = self.db.query(WorkerJob).filter(WorkerJob.id == jid).first()
                        if worker_job and worker_job.status in ("success", "failed"):
                            already_completed += 1
                            # Trigger immediate barrier check for this job
                            asyncio.create_task(
                                self._trigger_immediate_barrier_check(
                                    run_id=run.id,
                                    job_id=jid,
                                    worker_job=worker_job,
                                ),
                                context=contextvars.Context(),
                            )

                    if already_completed:
                        logger.info(f"{already_completed}/{len(job_ids)} workers already completed - scheduled barrier checks")

                else:
                    # SINGLE-WORKER PATH (backwards compatibility)
                    # Commit WAITING status now (no barrier needed for single worker)
                    self.db.commit()

                    job_id = interrupt_value.get("job_id") if isinstance(interrupt_value, dict) else None
                    interrupt_message = (
                        interrupt_value.get("message", "Working on this in the background...")
                        if isinstance(interrupt_value, dict)
                        else str(interrupt_value)
                    )

                    # RACE SAFETY: Check if worker already completed while we were setting up.
                    # This handles the case where worker finished before WAITING was committed,
                    # and its retry loop gave up. We immediately trigger resume if so.
                    if job_id:
                        worker_job = self.db.query(WorkerJob).filter(WorkerJob.id == job_id).first()
                        if worker_job and worker_job.status in ("success", "failed"):
                            logger.info(
                                f"Worker job {job_id} already completed ({worker_job.status}) "
                                f"while supervisor was setting up - scheduling immediate resume"
                            )
                            # Schedule resume in background (don't block)
                            # Use empty context to avoid leaking supervisor context vars
                            asyncio.create_task(
                                self._trigger_immediate_resume(
                                    run_id=run.id,
                                    worker_job=worker_job,
                                ),
                                context=contextvars.Context(),
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
                        "trace_id": effective_trace_id,
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
                reset_supervisor_context(_supervisor_ctx_token)
                reset_emitter(_emitter_token)
                # Reset user context
                from zerg.callbacks.token_stream import current_user_id_var

                current_user_id_var.reset(_user_ctx_token)

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
            # has been removed. With the interrupt/resume pattern, spawn_worker raises
            # AgentInterrupted before we get here. See the AgentInterrupted handler above.

            # Update run status
            run.status = RunStatus.SUCCESS
            run.finished_at = end_time.replace(tzinfo=None)
            run.duration_ms = duration_ms
            if runner.usage_total_tokens is not None:
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
                    "trace_id": str(run.trace_id) if run.trace_id else None,
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

            # Auto-summary -> Memory Files (async, best-effort)
            from zerg.services.memory_summarizer import schedule_run_summary

            schedule_run_summary(
                owner_id=owner_id,
                thread_id=thread.id,
                run_id=run.id,
                task=task,
                result_text=result_text or "",
                trace_id=str(run.trace_id) if run.trace_id else None,
            )

            # Cloud execution notification (best-effort)
            from zerg.services.ops_discord import send_run_completion_notification

            await send_run_completion_notification(
                run_id=run.id,
                status="success",
                summary=result_text[:500] if result_text else None,
                run_url=f"https://swarmlet.com/runs/{run.id}",
            )

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
                    "trace_id": str(run.trace_id) if run.trace_id else None,
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

            # Mark barrier as failed if it exists (prevents stuck state)
            from zerg.models.worker_barrier import WorkerBarrier

            barrier = self.db.query(WorkerBarrier).filter(WorkerBarrier.run_id == run.id).first()
            if barrier and barrier.status not in ("completed", "failed"):
                barrier.status = "failed"

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
                    "trace_id": str(run.trace_id) if run.trace_id else None,
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

            # Cloud execution notification (best-effort)
            from zerg.services.ops_discord import send_run_completion_notification

            await send_run_completion_notification(
                run_id=run.id,
                status="failed",
                error=str(e),
                run_url=f"https://swarmlet.com/runs/{run.id}",
            )

            logger.exception(f"Supervisor run {run.id} failed: {e}")

            return SupervisorRunResult(
                run_id=run.id,
                thread_id=thread.id,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
                debug_url=f"/supervisor/{run.id}",
            )

    async def _trigger_immediate_resume(self, run_id: int, worker_job: WorkerJob) -> None:
        """Trigger immediate resume when worker completed before supervisor entered WAITING.

        This handles the race condition where:
        1. Worker finishes fast (before supervisor commits WAITING)
        2. Worker's retry loop gives up after 2 seconds
        3. Supervisor commits WAITING
        4. → We detect completed worker and trigger resume immediately

        Parameters
        ----------
        run_id
            Supervisor run ID
        worker_job
            The completed WorkerJob record
        """
        from zerg.database import get_session_factory
        from zerg.services.worker_resume import resume_supervisor_with_worker_result

        try:

            def _extract_summary_from_result(result: str, max_chars: int = 400) -> str | None:
                text = (result or "").strip()
                if not text:
                    return None
                first_para = text.split("\n\n", 1)[0].strip()
                summary = first_para or text
                if len(summary) > max_chars:
                    return summary[:max_chars].rstrip() + "…"
                return summary

            def _truncate_result(result: str, max_chars: int = 2000) -> str:
                text = result or ""
                if len(text) <= max_chars:
                    return text
                return text[:max_chars].rstrip() + "\n\n… (truncated)"

            # Prefer the same "summary-first" resume payload used by the normal path.
            artifact_store = WorkerArtifactStore()
            result_text: str

            if worker_job.status == "failed":
                result_text = f"Worker failed: {worker_job.error or 'Unknown error'}"
            elif not worker_job.worker_id:
                result_text = f"Worker job {worker_job.id} completed ({worker_job.status})"
            else:
                summary = None
                try:
                    metadata = artifact_store.get_worker_metadata(worker_job.worker_id, owner_id=worker_job.owner_id)
                    summary = metadata.get("summary")
                except Exception:
                    summary = None

                if summary:
                    result_text = str(summary)
                else:
                    try:
                        full_result = artifact_store.get_worker_result(worker_job.worker_id)
                    except FileNotFoundError:
                        result_text = f"Worker completed but result not found (worker_id: {worker_job.worker_id})"
                    else:
                        extracted = _extract_summary_from_result(full_result)
                        result_text = extracted or _truncate_result(full_result)

            # Resume with fresh DB session
            session_factory = get_session_factory()
            fresh_db = session_factory()
            try:
                await resume_supervisor_with_worker_result(
                    db=fresh_db,
                    run_id=run_id,
                    worker_result=result_text,
                    job_id=worker_job.id,
                )
                logger.info(f"Immediate resume completed for run {run_id}")
            finally:
                fresh_db.close()

        except Exception as e:
            logger.exception(f"Failed to trigger immediate resume for run {run_id}: {e}")

    async def _trigger_immediate_barrier_check(self, run_id: int, job_id: int, worker_job: WorkerJob) -> None:
        """Trigger immediate barrier check when worker completed before barrier was set up.

        This handles the race condition for parallel workers where:
        1. Worker finishes fast (before barrier commits)
        2. Worker's retry loop gives up
        3. Barrier is created
        4. → We detect completed worker and trigger barrier check immediately

        Parameters
        ----------
        run_id
            Supervisor run ID
        job_id
            WorkerJob ID
        worker_job
            The completed WorkerJob record
        """
        from zerg.database import get_session_factory
        from zerg.services.worker_resume import check_and_resume_if_all_complete
        from zerg.services.worker_resume import resume_supervisor_batch

        try:
            # Extract result summary
            artifact_store = WorkerArtifactStore()
            result_text: str

            if worker_job.status == "failed":
                result_text = f"Worker failed: {worker_job.error or 'Unknown error'}"
            elif not worker_job.worker_id:
                result_text = f"Worker job {worker_job.id} completed ({worker_job.status})"
            else:
                summary = None
                try:
                    metadata = artifact_store.get_worker_metadata(worker_job.worker_id, owner_id=worker_job.owner_id)
                    summary = metadata.get("summary")
                except Exception:
                    summary = None

                if summary:
                    result_text = str(summary)
                else:
                    try:
                        full_result = artifact_store.get_worker_result(worker_job.worker_id)
                        # Truncate for barrier storage
                        result_text = full_result[:2000] if len(full_result) > 2000 else full_result
                    except FileNotFoundError:
                        result_text = f"Worker completed but result not found (worker_id: {worker_job.worker_id})"

            # Trigger barrier check with fresh DB session
            session_factory = get_session_factory()
            fresh_db = session_factory()
            try:
                barrier_result = await check_and_resume_if_all_complete(
                    db=fresh_db,
                    run_id=run_id,
                    job_id=job_id,
                    result=result_text,
                    error=worker_job.error if worker_job.status == "failed" else None,
                )

                # CRITICAL: Commit barrier state changes (nested transaction needs outer commit)
                fresh_db.commit()

                if barrier_result["status"] == "resume":
                    logger.info(
                        f"Immediate barrier check for run {run_id} triggered batch resume "
                        f"with {len(barrier_result['worker_results'])} results"
                    )
                    await resume_supervisor_batch(
                        db=fresh_db,
                        run_id=run_id,
                        worker_results=barrier_result["worker_results"],
                    )
                elif barrier_result["status"] == "waiting":
                    logger.info(
                        f"Immediate barrier check for run {run_id}: " f"{barrier_result['completed']}/{barrier_result['expected']} complete"
                    )
                else:
                    logger.debug(f"Immediate barrier check skipped for run {run_id}: " f"{barrier_result.get('reason')}")

            finally:
                fresh_db.close()

        except Exception as e:
            logger.exception(f"Failed to trigger immediate barrier check for run {run_id}: {e}")

    # NOTE: run_continuation() removed - replaced by LangGraph-free continuation
    # See worker_resume.py for the new implementation using AgentRunner.run_continuation()


__all__ = ["SupervisorService", "SupervisorRunResult", "SUPERVISOR_THREAD_TYPE"]
