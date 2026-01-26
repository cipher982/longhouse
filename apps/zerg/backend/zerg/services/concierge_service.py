"""Concierge Service - manages the "one brain per user" concierge lifecycle.

This service handles:
- Finding or creating the user's long-lived concierge thread
- Running the concierge fiche with streaming events
- Coordinating commis execution and result synthesis

The key invariant is ONE concierge thread per user that persists across sessions.
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
from zerg.managers.fiche_runner import CourseInterrupted
from zerg.managers.fiche_runner import FicheRunner
from zerg.models.enums import CourseStatus
from zerg.models.enums import ThreadType
from zerg.models.models import CommisJob
from zerg.models.models import Course
from zerg.models.models import Fiche as FicheModel
from zerg.models.models import Thread as ThreadModel
from zerg.prompts import build_concierge_prompt
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.concierge_context import reset_seq
from zerg.services.thread_service import ThreadService
from zerg.tools.builtin.concierge_tools import get_concierge_allowed_tools

logger = logging.getLogger(__name__)

# Thread type for concierge threads - distinguishes from regular fiche threads
CONCIERGE_THREAD_TYPE = ThreadType.SUPER

# Configuration for recent commis history injection
RECENT_COMMIS_HISTORY_LIMIT = 5  # Max commis to show
RECENT_COMMIS_HISTORY_MINUTES = 10  # Only show commis from last N minutes
# Marker to identify ephemeral context messages (for cleanup)
RECENT_COMMIS_CONTEXT_MARKER = "<!-- RECENT_COMMIS_CONTEXT -->"


@dataclass
class ConciergeCourseResult:
    """Result from a concierge course.

    Aligns with UI spec's ConciergeResult schema for frontend consumption.
    """

    course_id: int
    thread_id: int
    status: str  # 'success' | 'failed' | 'cancelled' | 'deferred' | 'error'
    result: str | None = None
    error: str | None = None
    duration_ms: int = 0
    debug_url: str | None = None  # Dashboard deep link


class ConciergeService:
    """Service for managing concierge fiche execution."""

    # Bump this whenever BASE_CONCIERGE_PROMPT meaningfully changes.
    CONCIERGE_PROMPT_VERSION = 2

    def __init__(self, db: Session):
        """Initialize the concierge service.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def get_or_create_concierge_fiche(self, owner_id: int) -> FicheModel:
        """Get or create the concierge fiche for a user.

        The concierge fiche is a special fiche with concierge tools enabled.
        Each user has exactly one concierge fiche.

        Args:
            owner_id: User ID

        Returns:
            The concierge fiche
        """
        from zerg.models_config import DEFAULT_MODEL_ID

        # Look for existing concierge fiche
        fiches = crud.get_fiches(self.db, owner_id=owner_id)
        for fiche in fiches:
            config = fiche.config or {}
            if config.get("is_concierge"):
                # Keep the concierge prompt and tool allowlist in sync with code.
                # Concierge fiches are system-managed; stale prompts routinely cause
                # "I searched but found nothing" hallucinations because the model is
                # running with outdated tool descriptions.
                changed = False
                user = crud.get_user(self.db, owner_id)
                if user:
                    desired_prompt = build_concierge_prompt(user)
                    if fiche.system_instructions != desired_prompt:
                        fiche.system_instructions = desired_prompt
                        changed = True

                # Use centralized tool list from concierge_tools.py (single source of truth)
                concierge_tools = get_concierge_allowed_tools()
                if fiche.allowed_tools != concierge_tools:
                    fiche.allowed_tools = concierge_tools
                    changed = True

                # Track prompt version in config for future migrations/debugging.
                if config.get("prompt_version") != self.CONCIERGE_PROMPT_VERSION:
                    config["prompt_version"] = self.CONCIERGE_PROMPT_VERSION
                    fiche.config = config
                    changed = True

                if changed:
                    self.db.commit()
                    self.db.refresh(fiche)

                logger.debug(f"Found existing concierge fiche {fiche.id} for user {owner_id}")
                return fiche

        # Create new concierge fiche
        logger.info(f"Creating concierge fiche for user {owner_id}")

        # Fetch user for context-aware prompt composition
        user = crud.get_user(self.db, owner_id)
        if not user:
            raise ValueError(f"User {owner_id} not found")

        concierge_config = {
            "is_concierge": True,
            "prompt_version": self.CONCIERGE_PROMPT_VERSION,
            "temperature": 0.7,
            "max_tokens": 2000,
            "reasoning_effort": "none",  # Disable reasoning for fast responses
        }

        # Use centralized tool list from concierge_tools.py (single source of truth)
        concierge_tools = get_concierge_allowed_tools()

        fiche = crud.create_fiche(
            db=self.db,
            owner_id=owner_id,
            name="Concierge",
            model=DEFAULT_MODEL_ID,
            system_instructions=build_concierge_prompt(user),
            task_instructions="You are helping the user accomplish their goals. " "Analyze their request and decide how to handle it.",
            config=concierge_config,
        )
        # Set allowed_tools (not supported in crud.create_fiche)
        fiche.allowed_tools = concierge_tools
        self.db.commit()
        self.db.refresh(fiche)

        logger.info(f"Created concierge fiche {fiche.id} for user {owner_id}")
        return fiche

    def get_or_create_concierge_thread(self, owner_id: int, fiche: FicheModel | None = None) -> ThreadModel:
        """Get or create the long-lived concierge thread for a user.

        Each user has exactly ONE concierge thread that persists across sessions.
        This implements the "one brain" pattern where context accumulates.

        Args:
            owner_id: User ID
            fiche: Optional concierge fiche (will be created if not provided)

        Returns:
            The concierge thread
        """
        if fiche is None:
            fiche = self.get_or_create_concierge_fiche(owner_id)

        # Look for existing concierge thread
        threads = crud.get_threads(self.db, fiche_id=fiche.id)
        for thread in threads:
            if thread.thread_type == CONCIERGE_THREAD_TYPE:
                logger.debug(f"Found existing concierge thread {thread.id} for user {owner_id}")
                return thread

        # Create new concierge thread
        logger.info(f"Creating concierge thread for user {owner_id}")

        thread = ThreadService.create_thread_with_system_message(
            self.db,
            fiche,
            title="Concierge",
            thread_type=CONCIERGE_THREAD_TYPE,
            active=True,
        )
        self.db.commit()

        logger.info(f"Created concierge thread {thread.id} for user {owner_id}")
        return thread

    def _build_recent_commis_context(self, owner_id: int) -> tuple[str | None, list[int]]:
        """Build inbox context with active commis and unacknowledged results.

        v3.0 (Async Inbox Model): The inbox shows:
        1. Active commis (queued, running) with elapsed time
        2. Unacknowledged completed commis with summaries

        This allows the concierge to be aware of background work without blocking.
        The message includes a marker for cleanup - see _cleanup_stale_commis_context().

        IMPORTANT: This method does NOT commit acknowledgements. It returns the job IDs
        that should be acknowledged. The caller must:
        1. Persist the system message to the thread
        2. THEN call _acknowledge_commis_jobs(job_ids) to mark them as seen

        This ensures atomic "see message + acknowledge" semantics.

        Returns:
            Tuple of (context_string, job_ids_to_acknowledge).
            context_string is None if there are no commis to show.
        """
        from datetime import timedelta

        # Query active commis (queued, running)
        active_jobs = (
            self.db.query(CommisJob)
            .filter(
                CommisJob.owner_id == owner_id,
                CommisJob.status.in_(["queued", "running"]),
            )
            .order_by(CommisJob.created_at.desc())
            .limit(RECENT_COMMIS_HISTORY_LIMIT)
            .all()
        )

        # Query unacknowledged completed commis (for inbox model)
        # These are results the concierge hasn't seen yet
        unacknowledged_jobs = (
            self.db.query(CommisJob)
            .filter(
                CommisJob.owner_id == owner_id,
                CommisJob.status.in_(["success", "failed"]),
                CommisJob.acknowledged == False,  # noqa: E712
            )
            .order_by(CommisJob.created_at.desc())
            .limit(RECENT_COMMIS_HISTORY_LIMIT)
            .all()
        )

        # Also include recent acknowledged jobs for context (last N minutes)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=RECENT_COMMIS_HISTORY_MINUTES)
        recent_acknowledged_jobs = (
            self.db.query(CommisJob)
            .filter(
                CommisJob.owner_id == owner_id,
                CommisJob.status.in_(["success", "failed"]),
                CommisJob.acknowledged == True,  # noqa: E712
                CommisJob.created_at >= cutoff,
            )
            .order_by(CommisJob.created_at.desc())
            .limit(3)  # Just show a few recent acknowledged for context
            .all()
        )

        if not active_jobs and not unacknowledged_jobs and not recent_acknowledged_jobs:
            return None, []

        # Try to get artifact store for richer summaries, but don't fail if unavailable
        artifact_store = None
        try:
            artifact_store = CommisArtifactStore()
        except (OSError, PermissionError) as e:
            logger.warning(f"CommisArtifactStore unavailable, using task summaries only: {e}")

        def get_elapsed_str(job_time: datetime) -> str:
            """Calculate elapsed time string."""
            if job_time.tzinfo is None:
                job_time = job_time.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - job_time
            if elapsed.total_seconds() >= 3600:
                return f"{int(elapsed.total_seconds() / 3600)}h ago"
            elif elapsed.total_seconds() >= 60:
                return f"{int(elapsed.total_seconds() / 60)}m ago"
            else:
                return f"{int(elapsed.total_seconds())}s ago"

        def get_summary(job: CommisJob, max_chars: int = 150) -> str:
            """Get summary from artifact store or truncate task."""
            summary = None
            if artifact_store and job.commis_id and job.status in ["success", "failed"]:
                try:
                    metadata = artifact_store.get_commis_metadata(job.commis_id)
                    summary = metadata.get("summary")
                except Exception:
                    pass
            if not summary:
                summary = job.task[:max_chars] + "..." if len(job.task) > max_chars else job.task
            return summary

        # Build context with marker for cleanup
        lines = [
            RECENT_COMMIS_CONTEXT_MARKER,  # Marker for identifying ephemeral context
            "## Commis Inbox",
        ]

        # Section 1: Active commis
        if active_jobs:
            lines.append("\n**Active Commis:**")
            for job in active_jobs:
                elapsed_str = get_elapsed_str(job.started_at or job.created_at)
                status_icon = "⏳" if job.status == "queued" else "⋯"
                task_preview = job.task[:80] + "..." if len(job.task) > 80 else job.task
                lines.append(f"- Job {job.id} [{status_icon} {job.status.upper()}] ({elapsed_str})")
                lines.append(f"  Task: {task_preview}")

        # Section 2: New results (unacknowledged)
        # Collect job IDs to acknowledge (caller will commit after message is persisted)
        jobs_to_acknowledge: list[int] = []
        if unacknowledged_jobs:
            lines.append("\n**New Results (unread):**")
            for job in unacknowledged_jobs:
                elapsed_str = get_elapsed_str(job.finished_at or job.created_at)
                status_icon = "✓" if job.status == "success" else "✗"
                summary = get_summary(job)
                lines.append(f"- Job {job.id} [{status_icon} {job.status.upper()}] ({elapsed_str})")
                lines.append(f"  {summary}")
                jobs_to_acknowledge.append(job.id)

        # Section 3: Recent acknowledged (brief reference only)
        if recent_acknowledged_jobs and not unacknowledged_jobs:
            lines.append("\n**Recent Work:**")
            for job in recent_acknowledged_jobs:
                elapsed_str = get_elapsed_str(job.finished_at or job.created_at)
                status_icon = "✓" if job.status == "success" else "✗"
                task_preview = job.task[:60] + "..." if len(job.task) > 60 else job.task
                lines.append(f"- Job {job.id} [{status_icon}] {task_preview} ({elapsed_str})")

        # Footer with usage hints
        lines.append("")
        if unacknowledged_jobs:
            lines.append("Use `read_commis_result(job_id)` for full details.")
        if active_jobs:
            lines.append("Use `check_commis_status()` to see commis progress.")
            lines.append("Use `wait_for_commis(job_id)` if you need to block for a result.")

        return "\n".join(lines), jobs_to_acknowledge

    def _acknowledge_commis_jobs(self, job_ids: list[int]) -> None:
        """Mark commis jobs as acknowledged after system message is persisted.

        This should be called AFTER the inbox context message is successfully
        persisted to the thread. This ensures atomic "see message + acknowledge" semantics.

        Args:
            job_ids: List of CommisJob IDs to mark as acknowledged
        """
        if not job_ids:
            return

        self.db.query(CommisJob).filter(CommisJob.id.in_(job_ids)).update(
            {"acknowledged": True},
            synchronize_session=False,
        )
        self.db.commit()
        logger.debug(f"Marked {len(job_ids)} commis jobs as acknowledged")

    def _cleanup_stale_commis_context(self, thread_id: int, min_age_seconds: float = 5.0) -> int:
        """Delete previous recent commis context messages from the thread.

        This prevents stale context from accumulating across runs.
        Messages are identified by the RECENT_COMMIS_CONTEXT_MARKER.

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
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
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
            logger.debug(f"Cleaned up {count} stale commis context message(s) from thread {thread_id}")

        return count

    async def run_concierge(
        self,
        owner_id: int,
        task: str,
        course_id: int | None = None,
        message_id: str | None = None,
        trace_id: str | None = None,
        timeout: int = 60,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        return_on_deferred: bool = True,
    ) -> ConciergeCourseResult:
        """Run the concierge fiche with a task.

        This method:
        1. Gets or creates the concierge thread for the user
        2. Uses existing course record OR creates a new one
        3. Adds the task as a user message
        4. Runs the concierge fiche
        5. Returns the result

        Args:
            owner_id: User ID
            task: The task/question from the user
            course_id: Optional existing course ID (avoids duplicate course creation)
            message_id: Client-generated message ID (UUID). If None, one will be generated.
            timeout: Maximum execution time in seconds
            model_override: Optional model to use instead of fiche's default
            reasoning_effort: Optional reasoning effort (none, low, medium, high)
            return_on_deferred: If True, return a DEFERRED response once the timeout hits.
                If False, emit CONCIERGE_DEFERRED but continue running in the background until completion.

        Returns:
            ConciergeCourseResult with course details and result
        """
        start_time = datetime.now(timezone.utc)
        started_at_naive = start_time.replace(tzinfo=None)

        # Get or create concierge components
        fiche = self.get_or_create_concierge_fiche(owner_id)

        # Always refresh concierge prompt from current templates + user context.
        # The concierge fiche is long-lived; without this, prompt updates (and user profile changes)
        # won't take effect until the fiche row is recreated.
        user = crud.get_user(self.db, owner_id)
        if not user:
            raise ValueError(f"User {owner_id} not found")
        fiche.system_instructions = build_concierge_prompt(user)
        self.db.commit()
        logger.debug(f"Refreshed concierge prompt for fiche {fiche.id} (user {owner_id})")
        thread = self.get_or_create_concierge_thread(owner_id, fiche)

        # Use existing course or create new one
        if course_id:
            course = self.db.query(Course).filter(Course.id == course_id).first()
            if not course:
                raise ValueError(f"Course {course_id} not found")
            logger.info(f"Using existing concierge course {course.id}", extra={"tag": "CONCIERGE"})
        else:
            # Create course record (fallback for direct calls)
            from zerg.models.enums import CourseTrigger

            course = Course(
                fiche_id=fiche.id,
                thread_id=thread.id,
                status=CourseStatus.RUNNING,
                trigger=CourseTrigger.API,
                started_at=started_at_naive,
                model=model_override or fiche.model,
                reasoning_effort=reasoning_effort,
            )
            self.db.add(course)
            self.db.commit()
            self.db.refresh(course)
            logger.info(f"Created new concierge course {course.id}", extra={"tag": "CONCIERGE"})

        # Ensure started_at is populated for existing courses as well.
        if course.started_at is None:
            course.started_at = started_at_naive
            self.db.commit()

        logger.info(
            f"Starting concierge course {course.id} for user {owner_id}, task: {task[:50]}...",
            extra={"tag": "CONCIERGE"},
        )

        # Use client-provided message_id or generate one (for direct API calls / tests)
        # This ID is stable across concierge_started -> concierge_token -> concierge_complete
        if message_id is None:
            message_id = str(uuid.uuid4())

        # Persist message_id to the course if not already set (jarvis_chat.py sets it on Course creation)
        if course.assistant_message_id != message_id:
            course.assistant_message_id = message_id
            self.db.commit()

        # Check if this is a continuation course (processing commis result from a deferred course)
        # If so, include continuation_of_message_id so frontend creates a NEW message bubble
        # instead of overwriting the original "delegating to commis" message
        is_continuation = course.continuation_of_course_id is not None
        continuation_of_message_id = None
        if is_continuation:
            # Look up the original course's assistant_message_id for proper UUID compliance
            original_course = self.db.query(Course).filter(Course.id == course.continuation_of_course_id).first()
            if original_course and original_course.assistant_message_id:
                continuation_of_message_id = original_course.assistant_message_id
            else:
                # Fallback: generate a new UUID if original course's message_id is not available
                # This maintains schema compliance (UUID format) while still signaling continuation
                continuation_of_message_id = str(uuid.uuid4())
                logger.warning(
                    f"Original course {course.continuation_of_course_id} has no assistant_message_id, "
                    f"using generated UUID {continuation_of_message_id}"
                )

        # Emit concierge started event
        from zerg.services.event_store import emit_course_event

        # Resolve trace_id early for downstream event payloads and context
        effective_trace_id = trace_id or (str(course.trace_id) if course.trace_id else None)
        if not effective_trace_id:
            effective_trace_id = str(uuid.uuid4())
            # Persist to course for consistency
            course.trace_id = uuid.UUID(effective_trace_id)
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

        await emit_course_event(
            db=self.db,
            course_id=course.id,
            event_type="concierge_started",
            payload=started_payload,
        )

        try:
            # v2.0: Inject recent commis history context before user message
            # This prevents redundant commis spawns by showing the concierge
            # what work has been done recently
            #
            # IMPORTANT: Clean up any stale context messages first to prevent
            # accumulation of outdated "X minutes ago" timestamps
            self._cleanup_stale_commis_context(thread.id)

            recent_commis_context, jobs_to_acknowledge = self._build_recent_commis_context(owner_id)
            if recent_commis_context:
                logger.debug(f"Injecting recent commis context for user {owner_id}")
                crud.create_thread_message(
                    db=self.db,
                    thread_id=thread.id,
                    role="system",
                    content=recent_commis_context,
                    processed=True,  # Mark as processed so fiche doesn't re-process
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

            # Acknowledge commis jobs AFTER messages are persisted (atomic semantics)
            # This ensures jobs aren't marked "seen" unless concierge actually sees them
            if jobs_to_acknowledge:
                self._acknowledge_commis_jobs(jobs_to_acknowledge)

            # Emit thinking event
            await emit_course_event(
                db=self.db,
                course_id=course.id,
                event_type="concierge_thinking",
                payload={
                    "message": "Analyzing your request...",
                    "owner_id": owner_id,
                    "trace_id": effective_trace_id,
                },
            )

            # Set concierge course context for spawn_commis correlation and tool event emission
            from zerg.services.concierge_context import reset_concierge_context
            from zerg.services.concierge_context import set_concierge_context

            _concierge_ctx_token = set_concierge_context(
                course_id=course.id,
                owner_id=owner_id,
                message_id=message_id,
                trace_id=effective_trace_id,
                model=model_override or fiche.model,
                reasoning_effort=reasoning_effort,
            )

            # Set up injected emitter for event emission (Phase 2 of emitter refactor)
            # ConciergeEmitter always emits concierge_tool_* events regardless of contextvar state
            # Note: Emitter does NOT hold a DB session - event emission opens its own session
            from zerg.events import ConciergeEmitter
            from zerg.events import reset_emitter
            from zerg.events import set_emitter

            _concierge_emitter = ConciergeEmitter(
                course_id=course.id,
                owner_id=owner_id,
                message_id=message_id,
                trace_id=effective_trace_id,
            )
            _emitter_token = set_emitter(_concierge_emitter)

            # Set user context for token streaming (required for real-time SSE tokens)
            from zerg.callbacks.token_stream import set_current_user_id

            _user_ctx_token = set_current_user_id(owner_id)

            # Run the fiche with timeout (shielded so timeout doesn't cancel work)
            runner = FicheRunner(fiche, model_override=model_override, reasoning_effort=reasoning_effort)
            run_task = asyncio.create_task(runner.run_thread(self.db, thread))
            try:
                # asyncio.shield() prevents timeout from cancelling the task -
                # the timeout stops WAITING, not the WORK itself
                created_messages = await asyncio.wait_for(
                    asyncio.shield(run_task),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Timeout migration: course continues in background, we return deferred status
                # Calculate duration for the deferred event
                end_time = datetime.now(timezone.utc)
                duration_ms = int((end_time - start_time).total_seconds() * 1000)

                # Update course status to DEFERRED (not FAILED)
                course.status = CourseStatus.DEFERRED
                course.duration_ms = duration_ms
                self.db.commit()

                # Emit deferred event (not error)
                await emit_course_event(
                    db=self.db,
                    course_id=course.id,
                    event_type="concierge_deferred",
                    payload={
                        "fiche_id": fiche.id,
                        "thread_id": thread.id,
                        "message": "Still working on this in the background. I'll continue when ready.",
                        "timeout_seconds": timeout,
                        "attach_url": f"/api/stream/courses/{course.id}",
                        "owner_id": owner_id,
                        "message_id": message_id,
                        "trace_id": effective_trace_id,
                    },
                )

                # v2.2: Also emit RUN_UPDATED for dashboard visibility
                await emit_course_event(
                    db=self.db,
                    course_id=course.id,
                    event_type="course_updated",
                    payload={
                        "fiche_id": fiche.id,
                        "status": "deferred",
                        "thread_id": thread.id,
                        "owner_id": owner_id,
                    },
                )

                logger.info(f"Concierge course {course.id} deferred after {timeout}s timeout (continuing in background until completion)")

                if return_on_deferred:
                    # Return deferred result - NOT an error.
                    # Note: In the production HTTP flows, concierge runs are executed in a long-lived
                    # background task (see jarvis_concierge/jarvis_chat) and can pass
                    # return_on_deferred=False to keep the DB session alive until completion.
                    return ConciergeCourseResult(
                        course_id=course.id,
                        thread_id=thread.id,
                        status="deferred",
                        result="Still working on this in the background. I'll let you know when it's done.",
                        duration_ms=duration_ms,
                        debug_url=f"/concierge/{course.id}",
                    )

                # Background mode: keep awaiting the original run_task to completion, then mark the course
                # finished and persist the result (SSE streams can close on CONCIERGE_DEFERRED).
                created_messages = await run_task

            except CourseInterrupted as interrupt:
                # Concierge interrupt (spawn_commis waiting for commis completion)
                # Run state is persisted; we'll resume via FicheRunner.run_continuation
                end_time = datetime.now(timezone.utc)
                duration_ms = int((end_time - start_time).total_seconds() * 1000)

                # Update course status to WAITING (NOT committed yet - atomic with barrier creation)
                course.status = CourseStatus.WAITING
                course.duration_ms = duration_ms
                # Persist partial token usage before WAITING (will be added to on resume)
                if runner.usage_total_tokens is not None:
                    course.total_tokens = runner.usage_total_tokens
                # NOTE: DO NOT commit here - we need WAITING + barrier to be atomic

                # Extract interrupt payload
                interrupt_value = interrupt.interrupt_value

                # TWO-PHASE COMMIT: Handle parallel commis (barrier pattern)
                # CRITICAL: Barrier creation and WAITING status must be in SAME transaction
                # to prevent race where commis completes before barrier exists
                if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "commis_pending":
                    from zerg.models.commis_barrier import CommisBarrier
                    from zerg.models.commis_barrier import CommisBarrierJob

                    job_ids = interrupt_value.get("job_ids", [])
                    created_jobs = interrupt_value.get("created_jobs", [])

                    logger.info(f"TWO-PHASE COMMIT: Creating barrier for {len(job_ids)} commis")

                    # PHASE 2: Create barrier FIRST (jobs are already created with status='created')
                    barrier = CommisBarrier(
                        course_id=course.id,
                        expected_count=len(job_ids),
                        status="waiting",
                        # Set deadline 10 minutes from now (configurable)
                        deadline_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
                    )
                    self.db.add(barrier)
                    self.db.flush()  # Get barrier.id

                    # Create CommisBarrierJob records with tool_call_id mapping
                    for job_info in created_jobs:
                        job = job_info["job"]
                        tool_call_id = job_info["tool_call_id"]
                        self.db.add(
                            CommisBarrierJob(
                                barrier_id=barrier.id,
                                job_id=job.id,
                                tool_call_id=tool_call_id,
                                status="queued",  # Ready for pickup
                            )
                        )

                    # PHASE 3: Flip jobs from 'created' to 'queued' (commis can now pick them up)
                    for job_id in job_ids:
                        self.db.query(CommisJob).filter(
                            CommisJob.id == job_id,
                            CommisJob.status == "created",
                        ).update({"status": "queued"})

                    self.db.commit()
                    logger.info(f"TWO-PHASE COMMIT complete: barrier={barrier.id}, {len(job_ids)} jobs queued")

                    # Emit commis_spawned events for UI (job_id → tool_call_id mapping)
                    # Must emit AFTER jobs are queued (UI expects valid jobs)
                    from zerg.services.event_store import append_course_event

                    for job_info in created_jobs:
                        job = job_info["job"]
                        tool_call_id = job_info["tool_call_id"]
                        task = job_info.get("task", job.task[:100] if job.task else "")
                        await append_course_event(
                            course_id=course.id,
                            event_type="commis_spawned",
                            payload={
                                "job_id": job.id,
                                "tool_call_id": tool_call_id,
                                "task": task,
                                "model": job.model,
                                "owner_id": owner_id,
                                "trace_id": effective_trace_id,
                            },
                        )
                    logger.info(f"Emitted {len(created_jobs)} commis_spawned events")

                    # For backwards compatibility with event payload
                    job_id = job_ids[0] if job_ids else None
                    interrupt_message = f"Working on {len(job_ids)} tasks in the background..."

                    # Check if any commis already completed (race safety)
                    already_completed = 0
                    for jid in job_ids:
                        commis_job = self.db.query(CommisJob).filter(CommisJob.id == jid).first()
                        if commis_job and commis_job.status in ("success", "failed"):
                            already_completed += 1
                            # Trigger immediate barrier check for this job
                            asyncio.create_task(
                                self._trigger_immediate_barrier_check(
                                    course_id=course.id,
                                    job_id=jid,
                                    commis_job=commis_job,
                                ),
                                context=contextvars.Context(),
                            )

                    if already_completed:
                        logger.info(f"{already_completed}/{len(job_ids)} commis already completed - scheduled barrier checks")

                else:
                    # SINGLE-COMMIS PATH (wait_for_commis or backwards compatibility)
                    job_id = interrupt_value.get("job_id") if isinstance(interrupt_value, dict) else None
                    interrupt_message = (
                        interrupt_value.get("message", "Working on this in the background...")
                        if isinstance(interrupt_value, dict)
                        else str(interrupt_value)
                    )

                    # For wait_for_commis: store tool_call_id so resume uses it (not spawn_commis's)
                    interrupt_type = interrupt_value.get("type") if isinstance(interrupt_value, dict) else None
                    if interrupt_type == "wait_for_commis":
                        wait_tool_call_id = interrupt_value.get("tool_call_id")
                        if wait_tool_call_id:
                            course.pending_tool_call_id = wait_tool_call_id
                            logger.debug(f"Stored pending_tool_call_id={wait_tool_call_id} for wait_for_commis")

                    # Commit WAITING status now (no barrier needed for single commis)
                    self.db.commit()

                    # RACE SAFETY: Check if commis already completed while we were setting up.
                    # This handles the case where commis finished before WAITING was committed,
                    # and its retry loop gave up. We immediately trigger resume if so.
                    if job_id:
                        commis_job = self.db.query(CommisJob).filter(CommisJob.id == job_id).first()
                        if commis_job and commis_job.status in ("success", "failed"):
                            logger.info(
                                f"Commis job {job_id} already completed ({commis_job.status}) "
                                f"while concierge was setting up - scheduling immediate resume"
                            )
                            # Schedule resume in background (don't block)
                            # Use empty context to avoid leaking concierge context vars
                            asyncio.create_task(
                                self._trigger_immediate_resume(
                                    course_id=course.id,
                                    commis_job=commis_job,
                                ),
                                context=contextvars.Context(),
                            )

                # Emit waiting event (similar to deferred but semantically different)
                await emit_course_event(
                    db=self.db,
                    course_id=course.id,
                    event_type="concierge_waiting",
                    payload={
                        "fiche_id": fiche.id,
                        "thread_id": thread.id,
                        "job_id": job_id,
                        "message": interrupt_message,
                        "owner_id": owner_id,
                        "message_id": message_id,
                        "close_stream": False,  # Keep SSE open for resume
                        "trace_id": effective_trace_id,
                    },
                )

                await emit_course_event(
                    db=self.db,
                    course_id=course.id,
                    event_type="course_updated",
                    payload={
                        "fiche_id": fiche.id,
                        "status": "waiting",
                        "thread_id": thread.id,
                        "owner_id": owner_id,
                    },
                )

                logger.info(f"Concierge course {course.id} interrupted (WAITING for commis job {job_id})")

                return ConciergeCourseResult(
                    course_id=course.id,
                    thread_id=thread.id,
                    status="waiting",
                    result=interrupt_message,
                    duration_ms=duration_ms,
                    debug_url=f"/concierge/{course.id}",
                )

            finally:
                # Always reset context and emitter even on timeout/deferred
                reset_concierge_context(_concierge_ctx_token)
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

            # NOTE: Old "durable runs" code that checked for commis spawns after completion
            # has been removed. With the interrupt/resume pattern, spawn_commis raises
            # CourseInterrupted before we get here. See the CourseInterrupted handler above.

            # Update course status
            course.status = CourseStatus.SUCCESS
            course.finished_at = end_time.replace(tzinfo=None)
            course.duration_ms = duration_ms
            if runner.usage_total_tokens is not None:
                course.total_tokens = runner.usage_total_tokens
            self.db.commit()

            # Emit completion event with ConciergeResult-aligned schema
            # Note: summary/recommendations/caveats would require parsing fiche response
            # For now, include required fields and let frontend extract details
            await emit_course_event(
                db=self.db,
                course_id=course.id,
                event_type="concierge_complete",
                payload={
                    "fiche_id": fiche.id,
                    "thread_id": thread.id,
                    "result": result_text or "(No result)",
                    "status": "success",
                    "duration_ms": duration_ms,
                    "debug_url": f"/concierge/{course.id}",
                    "owner_id": owner_id,
                    "message_id": message_id,
                    "trace_id": str(course.trace_id) if course.trace_id else None,
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
            await emit_course_event(
                db=self.db,
                course_id=course.id,
                event_type="course_updated",
                payload={
                    "fiche_id": fiche.id,
                    "status": "success",
                    "finished_at": end_time.isoformat(),
                    "duration_ms": duration_ms,
                    "thread_id": thread.id,
                    "owner_id": owner_id,
                },
            )
            reset_seq(course.id)

            # Auto-summary -> Memory Files (async, best-effort)
            from zerg.services.memory_summarizer import schedule_course_summary

            schedule_course_summary(
                owner_id=owner_id,
                thread_id=thread.id,
                course_id=course.id,
                task=task,
                result_text=result_text or "",
                trace_id=str(course.trace_id) if course.trace_id else None,
            )

            # Cloud execution notification (best-effort)
            from zerg.services.ops_discord import send_course_completion_notification

            await send_course_completion_notification(
                course_id=course.id,
                status="success",
                summary=result_text[:500] if result_text else None,
                course_url=f"https://swarmlet.com/courses/{course.id}",
            )

            logger.info(f"Concierge course {course.id} completed in {duration_ms}ms", extra={"tag": "CONCIERGE"})

            return ConciergeCourseResult(
                course_id=course.id,
                thread_id=thread.id,
                status="success",
                result=result_text,
                duration_ms=duration_ms,
                debug_url=f"/concierge/{course.id}",
            )

        except asyncio.CancelledError:
            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update course status to cancelled if not already terminal
            if course.status not in {CourseStatus.CANCELLED, CourseStatus.SUCCESS, CourseStatus.FAILED}:
                course.status = CourseStatus.CANCELLED
                course.finished_at = end_time.replace(tzinfo=None)
                course.duration_ms = duration_ms
                self.db.commit()

            await emit_course_event(
                db=self.db,
                course_id=course.id,
                event_type="concierge_complete",
                payload={
                    "fiche_id": fiche.id,
                    "thread_id": thread.id,
                    "status": "cancelled",
                    "duration_ms": duration_ms,
                    "owner_id": owner_id,
                    "trace_id": str(course.trace_id) if course.trace_id else None,
                },
            )

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_course_event(
                db=self.db,
                course_id=course.id,
                event_type="course_updated",
                payload={
                    "fiche_id": fiche.id,
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

            # Update course status
            course.status = CourseStatus.FAILED
            course.finished_at = end_time.replace(tzinfo=None)
            course.duration_ms = duration_ms
            course.error = str(e)

            # Mark barrier as failed if it exists (prevents stuck state)
            from zerg.models.commis_barrier import CommisBarrier

            barrier = self.db.query(CommisBarrier).filter(CommisBarrier.course_id == course.id).first()
            if barrier and barrier.status not in ("completed", "failed"):
                barrier.status = "failed"

            self.db.commit()

            # Emit error event with consistent schema
            await emit_course_event(
                db=self.db,
                course_id=course.id,
                event_type="error",
                payload={
                    "fiche_id": fiche.id,
                    "thread_id": thread.id,
                    "message": str(e),
                    "status": "error",
                    "debug_url": f"/concierge/{course.id}",
                    "owner_id": owner_id,
                    "trace_id": str(course.trace_id) if course.trace_id else None,
                },
            )

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_course_event(
                db=self.db,
                course_id=course.id,
                event_type="course_updated",
                payload={
                    "fiche_id": fiche.id,
                    "status": "failed",
                    "finished_at": end_time.isoformat(),
                    "duration_ms": duration_ms,
                    "error": str(e),
                    "thread_id": thread.id,
                    "owner_id": owner_id,
                },
            )
            reset_seq(course.id)

            # Cloud execution notification (best-effort)
            from zerg.services.ops_discord import send_course_completion_notification

            await send_course_completion_notification(
                course_id=course.id,
                status="failed",
                error=str(e),
                course_url=f"https://swarmlet.com/courses/{course.id}",
            )

            logger.exception(f"Concierge course {course.id} failed: {e}")

            return ConciergeCourseResult(
                course_id=course.id,
                thread_id=thread.id,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
                debug_url=f"/concierge/{course.id}",
            )

    async def _trigger_immediate_resume(self, course_id: int, commis_job: CommisJob) -> None:
        """Trigger immediate resume when commis completed before concierge entered WAITING.

        This handles the race condition where:
        1. Commis finishes fast (before concierge commits WAITING)
        2. Commis's retry loop gives up after 2 seconds
        3. Concierge commits WAITING
        4. → We detect completed commis and trigger resume immediately

        Parameters
        ----------
        course_id
            Concierge course ID
        commis_job
            The completed CommisJob record
        """
        from zerg.database import get_session_factory
        from zerg.services.commis_resume import resume_concierge_with_commis_result

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
            artifact_store = CommisArtifactStore()
            result_text: str

            if commis_job.status == "failed":
                result_text = f"Commis failed: {commis_job.error or 'Unknown error'}"
            elif not commis_job.commis_id:
                result_text = f"Commis job {commis_job.id} completed ({commis_job.status})"
            else:
                summary = None
                try:
                    metadata = artifact_store.get_commis_metadata(commis_job.commis_id, owner_id=commis_job.owner_id)
                    summary = metadata.get("summary")
                except Exception:
                    summary = None

                if summary:
                    result_text = str(summary)
                else:
                    try:
                        full_result = artifact_store.get_commis_result(commis_job.commis_id)
                    except FileNotFoundError:
                        result_text = f"Commis completed but result not found (commis_id: {commis_job.commis_id})"
                    else:
                        extracted = _extract_summary_from_result(full_result)
                        result_text = extracted or _truncate_result(full_result)

            # Resume with fresh DB session
            session_factory = get_session_factory()
            fresh_db = session_factory()
            try:
                await resume_concierge_with_commis_result(
                    db=fresh_db,
                    course_id=course_id,
                    commis_result=result_text,
                    job_id=commis_job.id,
                )
                logger.info(f"Immediate resume completed for course {course_id}")
            finally:
                fresh_db.close()

        except Exception as e:
            logger.exception(f"Failed to trigger immediate resume for course {course_id}: {e}")

    async def _trigger_immediate_barrier_check(self, course_id: int, job_id: int, commis_job: CommisJob) -> None:
        """Trigger immediate barrier check when commis completed before barrier was set up.

        This handles the race condition for parallel commis where:
        1. Commis finishes fast (before barrier commits)
        2. Commis's retry loop gives up
        3. Barrier is created
        4. → We detect completed commis and trigger barrier check immediately

        Parameters
        ----------
        course_id
            Concierge course ID
        job_id
            CommisJob ID
        commis_job
            The completed CommisJob record
        """
        from zerg.database import get_session_factory
        from zerg.services.commis_resume import check_and_resume_if_all_complete
        from zerg.services.commis_resume import resume_concierge_batch

        try:
            # Extract result summary
            artifact_store = CommisArtifactStore()
            result_text: str

            if commis_job.status == "failed":
                result_text = f"Commis failed: {commis_job.error or 'Unknown error'}"
            elif not commis_job.commis_id:
                result_text = f"Commis job {commis_job.id} completed ({commis_job.status})"
            else:
                summary = None
                try:
                    metadata = artifact_store.get_commis_metadata(commis_job.commis_id, owner_id=commis_job.owner_id)
                    summary = metadata.get("summary")
                except Exception:
                    summary = None

                if summary:
                    result_text = str(summary)
                else:
                    try:
                        full_result = artifact_store.get_commis_result(commis_job.commis_id)
                        # Truncate for barrier storage
                        result_text = full_result[:2000] if len(full_result) > 2000 else full_result
                    except FileNotFoundError:
                        result_text = f"Commis completed but result not found (commis_id: {commis_job.commis_id})"

            # Trigger barrier check with fresh DB session
            session_factory = get_session_factory()
            fresh_db = session_factory()
            try:
                barrier_result = await check_and_resume_if_all_complete(
                    db=fresh_db,
                    course_id=course_id,
                    job_id=job_id,
                    result=result_text,
                    error=commis_job.error if commis_job.status == "failed" else None,
                )

                # CRITICAL: Commit barrier state changes (nested transaction needs outer commit)
                fresh_db.commit()

                if barrier_result["status"] == "resume":
                    logger.info(
                        f"Immediate barrier check for course {course_id} triggered batch resume "
                        f"with {len(barrier_result['commis_results'])} results"
                    )
                    await resume_concierge_batch(
                        db=fresh_db,
                        course_id=course_id,
                        commis_results=barrier_result["commis_results"],
                    )
                elif barrier_result["status"] == "waiting":
                    logger.info(
                        f"Immediate barrier check for course {course_id}: "
                        f"{barrier_result['completed']}/{barrier_result['expected']} complete"
                    )
                else:
                    logger.debug(f"Immediate barrier check skipped for course {course_id}: {barrier_result.get('reason')}")

            finally:
                fresh_db.close()

        except Exception as e:
            logger.exception(f"Failed to trigger immediate barrier check for course {course_id}: {e}")

    # NOTE: run_continuation() removed - replaced by LangGraph-free continuation
    # See commis_resume.py for the new implementation using FicheRunner.run_continuation()


__all__ = ["ConciergeService", "ConciergeCourseResult", "CONCIERGE_THREAD_TYPE"]
