"""Oikos Service - manages the "one brain per user" oikos lifecycle.

This service handles:
- Finding or creating the user's long-lived oikos thread
- Running the oikos fiche with streaming events
- Coordinating commis execution and result synthesis

The key invariant is ONE oikos thread per user that persists across sessions.
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
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.managers.fiche_runner import FicheInterrupted
from zerg.managers.fiche_runner import Runner
from zerg.models.enums import RunStatus
from zerg.models.enums import ThreadType
from zerg.models.models import CommisJob
from zerg.models.models import Fiche as FicheModel
from zerg.models.models import Run
from zerg.models.models import Thread as ThreadModel
from zerg.prompts import build_oikos_prompt
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.oikos_commis_context import RECENT_COMMIS_CONTEXT_MARKER
from zerg.services.oikos_commis_context import RECENT_COMMIS_HISTORY_LIMIT
from zerg.services.oikos_commis_context import RECENT_COMMIS_HISTORY_MINUTES
from zerg.services.oikos_commis_context import acknowledge_commis_jobs
from zerg.services.oikos_commis_context import build_recent_commis_context
from zerg.services.oikos_commis_context import cleanup_stale_commis_context
from zerg.services.oikos_context import reset_seq
from zerg.services.oikos_run_lifecycle import emit_cancelled_run_updated
from zerg.services.oikos_run_lifecycle import emit_error_event_and_close_stream
from zerg.services.oikos_run_lifecycle import emit_failed_run_updated
from zerg.services.oikos_run_lifecycle import emit_oikos_complete_success
from zerg.services.oikos_run_lifecycle import emit_oikos_waiting_and_run_updated
from zerg.services.oikos_run_lifecycle import emit_stream_control_for_pending_commiss
from zerg.services.oikos_run_lifecycle import emit_success_run_updated
from zerg.services.thread_service import ThreadService
from zerg.tools.builtin.oikos_tools import get_oikos_allowed_tools

if TYPE_CHECKING:
    from zerg.surfaces.base import SurfaceAdapter

logger = logging.getLogger(__name__)

# Thread type for oikos threads - distinguishes from regular fiche threads
OIKOS_THREAD_TYPE = ThreadType.SUPER
_OWNER_RUN_LOCKS: dict[int, asyncio.Lock] = {}
_OWNER_RUN_LOCKS_GUARD = asyncio.Lock()


async def _get_owner_run_lock(owner_id: int) -> asyncio.Lock:
    """Return (and lazily create) a process-local per-owner lock."""
    async with _OWNER_RUN_LOCKS_GUARD:
        lock = _OWNER_RUN_LOCKS.get(owner_id)
        if lock is None:
            lock = asyncio.Lock()
            _OWNER_RUN_LOCKS[owner_id] = lock
        return lock


def _normalize_assistant_message_id(message_id: str | None) -> str:
    """Return a canonical UUID string for persisted assistant message IDs."""
    if message_id is None:
        return str(uuid.uuid4())

    raw = str(message_id).strip()
    if not raw:
        raise ValueError("message_id must be a UUID")

    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise ValueError("message_id must be a UUID") from exc


@dataclass
class OikosRunResult:
    """Result from a oikos run.

    Aligns with UI spec's OikosResult schema for frontend consumption.
    """

    run_id: int
    thread_id: int
    status: str  # 'success' | 'failed' | 'cancelled' | 'deferred' | 'error'
    result: str | None = None
    error: str | None = None
    duration_ms: int = 0
    debug_url: str | None = None  # Dashboard deep link


async def emit_stream_control(
    db: Session,
    run: Run,
    action: str,  # "keep_open" | "close"
    reason: str,
    owner_id: int,
    ttl_ms: int | None = None,
) -> None:
    """Emit stream_control event for explicit stream lifecycle management.

    Only the oikos service should call this - single emitter pattern.

    Args:
        db: Database session
        run: Run instance
        action: "keep_open" (extends lease) or "close" (terminal)
        reason: Why this action (commiss_pending, continuation_start, all_complete, timeout, error)
        owner_id: Owner ID for security filtering
        ttl_ms: Optional lease time for keep_open (max 5 minutes)
    """
    from zerg.services.event_store import emit_run_event

    payload: dict = {
        "action": action,
        "reason": reason,
        "run_id": run.id,
        "owner_id": owner_id,
    }
    if ttl_ms:
        payload["ttl_ms"] = min(ttl_ms, 300_000)  # Cap at 5 min
    if run.trace_id:
        payload["trace_id"] = str(run.trace_id)

    # For keep_open, include pending commis count for debugging
    if action == "keep_open":
        pending_count = (
            db.query(CommisJob)
            .filter(
                CommisJob.oikos_run_id == run.id,
                CommisJob.status.in_(["queued", "running"]),
            )
            .count()
        )
        payload["pending_commiss"] = pending_count

    await emit_run_event(db=db, run_id=run.id, event_type="stream_control", payload=payload)
    logger.debug(f"Emitted stream_control:{action} (reason={reason}) for run {run.id}")


class OikosService:
    """Service for managing oikos fiche execution."""

    # Bump this whenever BASE_OIKOS_PROMPT meaningfully changes.
    OIKOS_PROMPT_VERSION = 4

    def __init__(self, db: Session):
        """Initialize the oikos service.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    @staticmethod
    def _build_surface_metadata(
        *,
        source_surface_id: str,
        source_conversation_id: str,
        source_message_id: str | None = None,
        source_event_id: str | None = None,
        source_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Build canonical message metadata for cross-surface rendering."""
        surface: dict[str, Any] = {
            "origin_surface_id": source_surface_id,
            "origin_conversation_id": source_conversation_id,
            # For same-surface turns, delivery defaults to origin.
            "delivery_surface_id": source_surface_id,
            "delivery_conversation_id": source_conversation_id,
            "visibility": "surface-local",
        }
        if source_message_id:
            surface["source_message_id"] = source_message_id
        if source_event_id:
            surface["source_event_id"] = source_event_id
        if source_idempotency_key:
            surface["idempotency_key"] = source_idempotency_key
        return {"surface": surface}

    @staticmethod
    def _merge_surface_metadata(existing: dict[str, Any] | None, surface_metadata: dict[str, Any]) -> dict[str, Any]:
        """Merge surface metadata without clobbering unrelated metadata (for example usage)."""
        merged = dict(existing or {})
        merged_surface = dict(merged.get("surface") or {})
        merged_surface.update(surface_metadata.get("surface") or {})
        merged["surface"] = merged_surface
        return merged

    def _annotate_assistant_messages_with_surface_metadata(
        self,
        created_messages: list[Any],
        surface_metadata: dict[str, Any],
    ) -> None:
        """Attach surface metadata to all assistant messages created in this run."""
        updated = False
        for row in created_messages:
            if getattr(row, "role", None) != "assistant":
                continue
            row.message_metadata = self._merge_surface_metadata(getattr(row, "message_metadata", None), surface_metadata)
            updated = True
        if updated:
            self.db.commit()

    def _annotate_assistant_messages_after_id(
        self,
        *,
        thread_id: int,
        min_message_id: int,
        surface_metadata: dict[str, Any],
    ) -> None:
        """Backfill surface metadata for assistant rows created after a known user message."""
        from zerg.models.thread import ThreadMessage as ThreadMessageModel

        rows = (
            self.db.query(ThreadMessageModel)
            .filter(
                ThreadMessageModel.thread_id == thread_id,
                ThreadMessageModel.id > min_message_id,
                ThreadMessageModel.role == "assistant",
            )
            .all()
        )
        if not rows:
            return
        for row in rows:
            row.message_metadata = self._merge_surface_metadata(row.message_metadata, surface_metadata)
        self.db.commit()

    def get_or_create_oikos_fiche(self, owner_id: int) -> FicheModel:
        """Get or create the oikos fiche for a user.

        The oikos fiche is a special fiche with oikos tools enabled.
        Each user has exactly one oikos fiche.

        Args:
            owner_id: User ID

        Returns:
            The oikos fiche
        """
        from zerg.models_config import DEFAULT_MODEL_ID

        # Look for existing oikos fiche
        fiches = crud.get_fiches(self.db, owner_id=owner_id)
        for fiche in fiches:
            config = fiche.config or {}
            if config.get("is_oikos"):
                # Keep the oikos prompt and tool allowlist in sync with code.
                # Oikos agents are system-managed; stale prompts routinely cause
                # "I searched but found nothing" hallucinations because the model is
                # running with outdated tool descriptions.
                changed = False
                user = crud.get_user(self.db, owner_id)
                if user:
                    desired_prompt = build_oikos_prompt(user)
                    if fiche.system_instructions != desired_prompt:
                        fiche.system_instructions = desired_prompt
                        changed = True

                # Use centralized tool list from oikos_tools.py (single source of truth)
                oikos_tools = get_oikos_allowed_tools()
                if fiche.allowed_tools != oikos_tools:
                    fiche.allowed_tools = oikos_tools
                    changed = True

                # Track prompt version in config for future migrations/debugging.
                if config.get("prompt_version") != self.OIKOS_PROMPT_VERSION:
                    config["prompt_version"] = self.OIKOS_PROMPT_VERSION
                    fiche.config = config
                    changed = True

                if changed:
                    self.db.commit()
                    self.db.refresh(fiche)

                logger.debug(f"Found existing oikos fiche {fiche.id} for user {owner_id}")
                return fiche

        # Create new oikos fiche
        logger.info(f"Creating oikos fiche for user {owner_id}")

        # Fetch user for context-aware prompt composition
        user = crud.get_user(self.db, owner_id)
        if not user:
            raise ValueError(f"User {owner_id} not found")

        oikos_config = {
            "is_oikos": True,
            "prompt_version": self.OIKOS_PROMPT_VERSION,
            "temperature": 0.7,
            "max_tokens": 2000,
            "reasoning_effort": "none",  # Disable reasoning for fast responses
        }

        # Use centralized tool list from oikos_tools.py (single source of truth)
        oikos_tools = get_oikos_allowed_tools()

        fiche = crud.create_fiche(
            db=self.db,
            owner_id=owner_id,
            name="Oikos",
            model=DEFAULT_MODEL_ID,
            system_instructions=build_oikos_prompt(user),
            task_instructions="You are helping the user accomplish their goals. " "Analyze their request and decide how to handle it.",
            config=oikos_config,
        )
        # Set allowed_tools (not supported in crud.create_fiche)
        fiche.allowed_tools = oikos_tools
        self.db.commit()
        self.db.refresh(fiche)

        logger.info(f"Created oikos fiche {fiche.id} for user {owner_id}")
        return fiche

    def get_or_create_oikos_thread(
        self,
        owner_id: int,
        fiche: FicheModel | None = None,
    ) -> ThreadModel:
        """Get or create the long-lived oikos thread for a user.

        Each user has exactly ONE oikos thread that persists across sessions.
        This implements the "one brain" pattern where context accumulates.

        Args:
            owner_id: User ID
            fiche: Optional oikos fiche (will be created if not provided)

        Returns:
            The oikos thread
        """
        if fiche is None:
            fiche = self.get_or_create_oikos_fiche(owner_id)

        # Look for existing oikos thread
        threads = crud.get_threads(self.db, fiche_id=fiche.id)
        for thread in threads:
            if thread.thread_type == OIKOS_THREAD_TYPE:
                logger.debug(f"Found existing oikos thread {thread.id} for user {owner_id}")
                return thread

        # Create new oikos thread
        logger.info(f"Creating oikos thread for user {owner_id}")

        thread = ThreadService.create_thread_with_system_message(
            self.db,
            fiche,
            title="Oikos",
            thread_type=OIKOS_THREAD_TYPE,
            active=True,
        )
        self.db.commit()

        logger.info(f"Created oikos thread {thread.id} for user {owner_id}")
        return thread

    def _build_recent_commis_context(self, owner_id: int) -> tuple[str | None, list[int]]:
        """Build inbox context with active commis and unread recent results."""
        return build_recent_commis_context(self.db, owner_id)

    def _acknowledge_commis_jobs(self, job_ids: list[int]) -> None:
        """Mark commis jobs as acknowledged after context message persistence."""
        acknowledge_commis_jobs(self.db, job_ids)

    def _cleanup_stale_commis_context(self, thread_id: int, min_age_seconds: float = 5.0) -> int:
        """Delete stale injected commis-context messages from the thread."""
        return cleanup_stale_commis_context(self.db, thread_id, min_age_seconds=min_age_seconds)

    async def run_oikos(
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
        source_surface_id: str = "web",
        source_conversation_id: str = "web:main",
        source_message_id: str | None = None,
        source_event_id: str | None = None,
        source_idempotency_key: str | None = None,
    ) -> OikosRunResult:
        """Run oikos with per-owner serialization to prevent cross-surface races."""
        owner_lock = await _get_owner_run_lock(owner_id)
        async with owner_lock:
            return await self._run_oikos_unlocked(
                owner_id=owner_id,
                task=task,
                run_id=run_id,
                message_id=message_id,
                trace_id=trace_id,
                timeout=timeout,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                return_on_deferred=return_on_deferred,
                source_surface_id=source_surface_id,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                source_event_id=source_event_id,
                source_idempotency_key=source_idempotency_key,
            )

    async def _run_oikos_unlocked(
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
        source_surface_id: str = "web",
        source_conversation_id: str = "web:main",
        source_message_id: str | None = None,
        source_event_id: str | None = None,
        source_idempotency_key: str | None = None,
    ) -> OikosRunResult:
        """Run the oikos fiche with a task.

        This method:
        1. Gets or creates the oikos thread for the user
        2. Uses existing run record OR creates a new one
        3. Adds the task as a user message
        4. Runs the oikos fiche
        5. Returns the result

        Args:
            owner_id: User ID
            task: The task/question from the user
            run_id: Optional existing run ID (avoids duplicate run creation)
            message_id: Client-generated message ID (UUID). If None, one will be generated.
            timeout: Maximum execution time in seconds
            model_override: Optional model to use instead of fiche's default
            reasoning_effort: Optional reasoning effort (none, low, medium, high)
            return_on_deferred: If True, return a DEFERRED response once the timeout hits.
                If False, emit OIKOS_DEFERRED but continue running in the background until completion.
            source_surface_id: Source surface for this turn (web, telegram, voice, system)
            source_conversation_id: Surface-native conversation identifier
            source_message_id: Source platform message ID (if available)
            source_event_id: Source platform event/update ID (if available)
            source_idempotency_key: Source idempotency key (if available)

        Returns:
            OikosRunResult with run details and result
        """
        start_time = datetime.now(timezone.utc)
        started_at_naive = start_time.replace(tzinfo=None)
        message_id = _normalize_assistant_message_id(message_id)
        surface_metadata = self._build_surface_metadata(
            source_surface_id=source_surface_id,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            source_event_id=source_event_id,
            source_idempotency_key=source_idempotency_key,
        )

        # Get or create oikos components
        fiche = self.get_or_create_oikos_fiche(owner_id)

        # Always refresh oikos prompt from current templates + user context.
        # The oikos fiche is long-lived; without this, prompt updates (and user profile changes)
        # won't take effect until the fiche row is recreated.
        user = crud.get_user(self.db, owner_id)
        if not user:
            raise ValueError(f"User {owner_id} not found")
        fiche.system_instructions = build_oikos_prompt(user)
        self.db.commit()
        logger.debug(f"Refreshed oikos prompt for fiche {fiche.id} (user {owner_id})")
        thread = self.get_or_create_oikos_thread(owner_id, fiche)

        # Use existing run or create new one
        if run_id:
            run = self.db.query(Run).filter(Run.id == run_id).first()
            if not run:
                raise ValueError(f"Run {run_id} not found")
            logger.info(f"Using existing oikos run {run.id}", extra={"tag": "OIKOS"})
        else:
            # Create run record (fallback for direct calls)
            from zerg.models.enums import RunTrigger

            run = Run(
                fiche_id=fiche.id,
                thread_id=thread.id,
                status=RunStatus.RUNNING,
                trigger=RunTrigger.API,
                started_at=started_at_naive,
                model=model_override or fiche.model,
                reasoning_effort=reasoning_effort,
            )
            self.db.add(run)
            self.db.commit()
            self.db.refresh(run)
            logger.info(f"Created new oikos run {run.id}", extra={"tag": "OIKOS"})

        # Ensure started_at is populated for existing runs as well.
        if run.started_at is None:
            run.started_at = started_at_naive
            self.db.commit()

        logger.info(f"Starting oikos run {run.id} for user {owner_id}, task: {task[:50]}...", extra={"tag": "OIKOS"})

        # Persist the normalized assistant message UUID for downstream events.
        persisted_message_id = str(run.assistant_message_id) if run.assistant_message_id else None
        if persisted_message_id != message_id:
            run.assistant_message_id = message_id
            self.db.commit()
            persisted_message_id = message_id
        # Always use the persisted message_id for downstream events.
        message_id = persisted_message_id or message_id

        # Check if this is a continuation run (processing commis result from a deferred run)
        # If so, include continuation_of_message_id so frontend creates a NEW message bubble
        # instead of overwriting the original "delegating to commis" message
        is_continuation = run.continuation_of_run_id is not None
        continuation_of_message_id = None
        if is_continuation:
            # Look up the original run's assistant_message_id for proper UUID compliance
            original_run = self.db.query(Run).filter(Run.id == run.continuation_of_run_id).first()
            if original_run and original_run.assistant_message_id:
                continuation_of_message_id = str(original_run.assistant_message_id)
            else:
                # Fallback: generate a new UUID if original run's message_id is not available
                # This maintains schema compliance (UUID format) while still signaling continuation
                continuation_of_message_id = str(uuid.uuid4())
                logger.warning(
                    f"Original run {run.continuation_of_run_id} has no assistant_message_id, "
                    f"using generated UUID {continuation_of_message_id}"
                )

        # Emit oikos started event
        from zerg.services.event_store import emit_run_event

        # Resolve trace_id early for downstream event payloads and context
        effective_trace_id = trace_id or (str(run.trace_id) if run.trace_id else None)
        if not effective_trace_id:
            effective_trace_id = str(uuid.uuid4())
            # Persist to run for consistency (GUID TypeDecorator handles UUID↔string)
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
            event_type="oikos_started",
            payload=started_payload,
        )

        try:
            # v2.0: Inject recent commis history context before user message
            # This prevents redundant commis spawns by showing the oikos
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
            user_message_row = crud.create_thread_message(
                db=self.db,
                thread_id=thread.id,
                role="user",
                content=task,
                processed=False,
                internal=is_continuation,  # Mark continuation prompts as internal
                message_metadata=surface_metadata,
            )
            self.db.commit()

            # Acknowledge commis jobs AFTER messages are persisted (atomic semantics)
            # This ensures jobs aren't marked "seen" unless oikos actually sees them
            if jobs_to_acknowledge:
                self._acknowledge_commis_jobs(jobs_to_acknowledge)

            # Emit thinking event
            await emit_run_event(
                db=self.db,
                run_id=run.id,
                event_type="oikos_thinking",
                payload={
                    "message": "Analyzing your request...",
                    "owner_id": owner_id,
                    "trace_id": effective_trace_id,
                },
            )

            # Set oikos run context for spawn_commis correlation and tool event emission
            from zerg.services.oikos_context import reset_oikos_context
            from zerg.services.oikos_context import set_oikos_context

            _oikos_ctx_token = set_oikos_context(
                run_id=run.id,
                owner_id=owner_id,
                message_id=message_id,
                trace_id=effective_trace_id,
                model=model_override or fiche.model,
                reasoning_effort=reasoning_effort,
            )

            # Set up injected emitter for event emission (Phase 2 of emitter refactor)
            # OikosEmitter always emits oikos_tool_* events regardless of contextvar state
            # Note: Emitter does NOT hold a DB session - event emission opens its own session
            from zerg.events import OikosEmitter
            from zerg.events import reset_emitter
            from zerg.events import set_emitter

            _oikos_emitter = OikosEmitter(
                run_id=run.id,
                owner_id=owner_id,
                message_id=message_id,
                trace_id=effective_trace_id,
            )
            _emitter_token = set_emitter(_oikos_emitter)

            # Set user context for token streaming (required for real-time SSE tokens)
            from zerg.callbacks.token_stream import set_current_user_id

            _user_ctx_token = set_current_user_id(owner_id)

            # Run the fiche with timeout (shielded so timeout doesn't cancel work)
            runner = Runner(fiche, model_override=model_override, reasoning_effort=reasoning_effort)
            run_task = asyncio.create_task(runner.run_thread(self.db, thread))
            try:
                # asyncio.shield() prevents timeout from cancelling the task -
                # the timeout stops WAITING, not the WORK itself
                created_messages = await asyncio.wait_for(
                    asyncio.shield(run_task),
                    timeout=timeout,
                )
                self._annotate_assistant_messages_with_surface_metadata(list(created_messages), surface_metadata)
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
                    event_type="oikos_deferred",
                    payload={
                        "fiche_id": fiche.id,
                        "thread_id": thread.id,
                        "message": "Still working on this in the background. I'll continue when ready.",
                        "timeout_seconds": timeout,
                        "attach_url": f"/api/oikos/runs/{run.id}/stream",
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
                        "fiche_id": fiche.id,
                        "status": "deferred",
                        "thread_id": thread.id,
                        "owner_id": owner_id,
                    },
                )

                logger.info(f"Oikos run {run.id} deferred after {timeout}s timeout (continuing in background until completion)")

                if return_on_deferred:
                    # Return deferred result - NOT an error.
                    # Note: In the production HTTP flows, oikos runs are executed in a long-lived
                    # background task (see oikos_oikos/oikos_chat) and can pass
                    # return_on_deferred=False to keep the DB session alive until completion.
                    return OikosRunResult(
                        run_id=run.id,
                        thread_id=thread.id,
                        status="deferred",
                        result="Still working on this in the background. I'll let you know when it's done.",
                        duration_ms=duration_ms,
                        debug_url=f"/oikos/{run.id}",
                    )

                # Background mode: keep awaiting the original run_task to completion, then mark the run
                # finished and persist the result (SSE streams can close on OIKOS_DEFERRED).
                created_messages = await run_task
                self._annotate_assistant_messages_with_surface_metadata(list(created_messages), surface_metadata)

            except FicheInterrupted as interrupt:
                self._annotate_assistant_messages_after_id(
                    thread_id=thread.id,
                    min_message_id=user_message_row.id,
                    surface_metadata=surface_metadata,
                )
                # Oikos interrupt (spawn_commis waiting for commis completion)
                # Run state is persisted; we'll resume via Runner.run_continuation
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

                # TWO-PHASE COMMIT: Handle parallel commiss (barrier pattern)
                # CRITICAL: Barrier creation and WAITING status must be in SAME transaction
                # to prevent race where commis completes before barrier exists
                if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "commiss_pending":
                    from zerg.models.commis_barrier import CommisBarrier
                    from zerg.models.commis_barrier import CommisBarrierJob

                    job_ids = interrupt_value.get("job_ids", [])
                    created_jobs = interrupt_value.get("created_jobs", [])

                    logger.info(f"TWO-PHASE COMMIT: Creating barrier for {len(job_ids)} commiss")

                    # PHASE 2: Create barrier FIRST (jobs are already created with status='created')
                    barrier = CommisBarrier(
                        run_id=run.id,
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

                    # PHASE 3: Flip jobs from 'created' to 'queued' (commiss can now pick them up)
                    for job_id in job_ids:
                        self.db.query(CommisJob).filter(
                            CommisJob.id == job_id,
                            CommisJob.status == "created",
                        ).update({"status": "queued"})

                    self.db.commit()
                    logger.info(f"TWO-PHASE COMMIT complete: barrier={barrier.id}, {len(job_ids)} jobs queued")

                    # Emit commis_spawned events for UI (job_id → tool_call_id mapping)
                    # Must emit AFTER jobs are queued (UI expects valid jobs)
                    from zerg.services.event_store import append_run_event

                    for job_info in created_jobs:
                        job = job_info["job"]
                        tool_call_id = job_info["tool_call_id"]
                        task = job_info.get("task", job.task[:100] if job.task else "")
                        await append_run_event(
                            run_id=run.id,
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

                    # Use first job_id for payload/UI convenience
                    job_id = job_ids[0] if job_ids else None
                    interrupt_message = f"Working on {len(job_ids)} tasks in the background..."

                    # Check if any commiss already completed (race safety)
                    already_completed = 0
                    for jid in job_ids:
                        commis_job = self.db.query(CommisJob).filter(CommisJob.id == jid).first()
                        if commis_job and commis_job.status in ("success", "failed"):
                            already_completed += 1
                            # Trigger immediate barrier check for this job
                            asyncio.create_task(
                                self._trigger_immediate_barrier_check(
                                    run_id=run.id,
                                    job_id=jid,
                                    commis_job=commis_job,
                                ),
                                context=contextvars.Context(),
                            )

                    if already_completed:
                        logger.info(f"{already_completed}/{len(job_ids)} commiss already completed - scheduled barrier checks")

                else:
                    # SINGLE-COMMIS PATH (wait_for_commis or single-commis interrupt)
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
                            run.pending_tool_call_id = wait_tool_call_id
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
                                f"while oikos was setting up - scheduling immediate resume"
                            )
                            # Schedule resume in background (don't block)
                            # Use empty context to avoid leaking oikos context vars
                            asyncio.create_task(
                                self._trigger_immediate_resume(
                                    run_id=run.id,
                                    commis_job=commis_job,
                                ),
                                context=contextvars.Context(),
                            )

                await emit_oikos_waiting_and_run_updated(
                    db=self.db,
                    run_id=run.id,
                    fiche_id=fiche.id,
                    thread_id=thread.id,
                    owner_id=owner_id,
                    message_id=message_id,
                    message=interrupt_message,
                    trace_id=effective_trace_id,
                    job_id=job_id,
                )

                logger.info(f"Oikos run {run.id} interrupted (WAITING for commis job {job_id})")

                return OikosRunResult(
                    run_id=run.id,
                    thread_id=thread.id,
                    status="waiting",
                    result=interrupt_message,
                    duration_ms=duration_ms,
                    debug_url=f"/oikos/{run.id}",
                )

            finally:
                # Always reset context and emitter even on timeout/deferred
                reset_oikos_context(_oikos_ctx_token)
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
            # FicheInterrupted before we get here. See the FicheInterrupted handler above.

            # Update run status
            run.status = RunStatus.SUCCESS
            run.finished_at = end_time.replace(tzinfo=None)
            run.duration_ms = duration_ms
            if runner.usage_total_tokens is not None:
                run.total_tokens = runner.usage_total_tokens
            self.db.commit()

            # Emit completion event with OikosResult-aligned schema
            # Note: summary/recommendations/caveats would require parsing fiche response
            # For now, include required fields and let frontend extract details
            await emit_oikos_complete_success(
                db=self.db,
                run_id=run.id,
                fiche_id=fiche.id,
                thread_id=thread.id,
                owner_id=owner_id,
                message_id=message_id,
                result=result_text or "(No result)",
                duration_ms=duration_ms,
                debug_url=f"/oikos/{run.id}",
                trace_id=str(run.trace_id) if run.trace_id else None,
                usage={
                    # Token usage for debug/power mode
                    "prompt_tokens": runner.usage_prompt_tokens,
                    "completion_tokens": runner.usage_completion_tokens,
                    "total_tokens": runner.usage_total_tokens,
                    "reasoning_tokens": runner.usage_reasoning_tokens,
                },
            )

            # Emit stream_control based on pending commiss
            await emit_stream_control_for_pending_commiss(self.db, run, owner_id, ttl_ms=120_000)

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_success_run_updated(
                db=self.db,
                run_id=run.id,
                fiche_id=fiche.id,
                thread_id=thread.id,
                owner_id=owner_id,
                finished_at_iso=end_time.isoformat(),
                duration_ms=duration_ms,
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
                run_url="https://longhouse.ai/timeline",
            )

            logger.info(f"Oikos run {run.id} completed in {duration_ms}ms", extra={"tag": "OIKOS"})

            return OikosRunResult(
                run_id=run.id,
                thread_id=thread.id,
                status="success",
                result=result_text,
                duration_ms=duration_ms,
                debug_url=f"/oikos/{run.id}",
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
                event_type="oikos_complete",
                payload={
                    "fiche_id": fiche.id,
                    "thread_id": thread.id,
                    "status": "cancelled",
                    "duration_ms": duration_ms,
                    "owner_id": owner_id,
                    "trace_id": str(run.trace_id) if run.trace_id else None,
                },
            )

            # Emit stream_control:close for cancelled runs
            await emit_stream_control(self.db, run, "close", "cancelled", owner_id)

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_cancelled_run_updated(
                db=self.db,
                run_id=run.id,
                fiche_id=fiche.id,
                thread_id=thread.id,
                owner_id=owner_id,
                finished_at_iso=end_time.isoformat(),
                duration_ms=duration_ms,
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
            from zerg.models.commis_barrier import CommisBarrier

            barrier = self.db.query(CommisBarrier).filter(CommisBarrier.run_id == run.id).first()
            if barrier and barrier.status not in ("completed", "failed"):
                barrier.status = "failed"

            self.db.commit()

            await emit_error_event_and_close_stream(
                db=self.db,
                run=run,
                thread_id=thread.id,
                owner_id=owner_id,
                message=str(e),
                trace_id=str(run.trace_id) if run.trace_id else None,
                fiche_id=fiche.id,
                debug_url=f"/oikos/{run.id}",
            )

            # v2.2: Also emit RUN_UPDATED for dashboard visibility
            await emit_failed_run_updated(
                db=self.db,
                run_id=run.id,
                fiche_id=fiche.id,
                thread_id=thread.id,
                owner_id=owner_id,
                finished_at_iso=end_time.isoformat(),
                duration_ms=duration_ms,
                error=str(e),
            )
            reset_seq(run.id)

            # Cloud execution notification (best-effort)
            from zerg.services.ops_discord import send_run_completion_notification

            await send_run_completion_notification(
                run_id=run.id,
                status="failed",
                error=str(e),
                run_url="https://longhouse.ai/timeline",
            )

            logger.exception(f"Oikos run {run.id} failed: {e}")

            return OikosRunResult(
                run_id=run.id,
                thread_id=thread.id,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
                debug_url=f"/oikos/{run.id}",
            )

    async def _trigger_immediate_resume(self, run_id: int, commis_job: CommisJob) -> None:
        """Trigger immediate resume when commis completed before oikos entered WAITING.

        This handles the race condition where:
        1. Commis finishes fast (before oikos commits WAITING)
        2. Commis's retry loop gives up after 2 seconds
        3. Oikos commits WAITING
        4. → We detect completed commis and trigger resume immediately

        Parameters
        ----------
        run_id
            Oikos run ID
        commis_job
            The completed CommisJob record
        """
        from zerg.database import get_session_factory
        from zerg.services.commis_single_resume import resume_oikos_with_commis_result

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
                from zerg.services.commis_runner import default_runner_factory

                await resume_oikos_with_commis_result(
                    db=fresh_db,
                    run_id=run_id,
                    commis_result=result_text,
                    job_id=commis_job.id,
                    runner_factory=default_runner_factory,
                )
                logger.info(f"Immediate resume completed for run {run_id}")
            finally:
                fresh_db.close()

        except Exception as e:
            logger.exception(f"Failed to trigger immediate resume for run {run_id}: {e}")

    async def _trigger_immediate_barrier_check(self, run_id: int, job_id: int, commis_job: CommisJob) -> None:
        """Trigger immediate barrier check when commis completed before barrier was set up.

        This handles the race condition for parallel commiss where:
        1. Commis finishes fast (before barrier commits)
        2. Commis's retry loop gives up
        3. Barrier is created
        4. → We detect completed commis and trigger barrier check immediately

        Parameters
        ----------
        run_id
            Oikos run ID
        job_id
            CommisJob ID
        commis_job
            The completed CommisJob record
        """
        from zerg.database import get_session_factory
        from zerg.services.commis_barrier import check_and_resume_if_all_complete
        from zerg.services.commis_batch_resume import resume_oikos_batch

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
                    run_id=run_id,
                    job_id=job_id,
                    result=result_text,
                    error=commis_job.error if commis_job.status == "failed" else None,
                )

                # CRITICAL: Commit barrier state changes (nested transaction needs outer commit)
                fresh_db.commit()

                if barrier_result["status"] == "resume":
                    logger.info(
                        f"Immediate barrier check for run {run_id} triggered batch resume "
                        f"with {len(barrier_result['commis_results'])} results"
                    )
                    await resume_oikos_batch(
                        db=fresh_db,
                        run_id=run_id,
                        commis_results=barrier_result["commis_results"],
                    )
                elif barrier_result["status"] == "waiting":
                    logger.info(
                        f"Immediate barrier check for run {run_id}: " f"{barrier_result['completed']}/{barrier_result['expected']} complete"
                    )
                else:
                    logger.debug(f"Immediate barrier check skipped for run {run_id}: {barrier_result.get('reason')}")

            finally:
                fresh_db.close()

        except Exception as e:
            logger.exception(f"Failed to trigger immediate barrier check for run {run_id}: {e}")


# ---------------------------------------------------------------------------
# Transport-agnostic invocation (the single entry point for all callers)
# ---------------------------------------------------------------------------


@dataclass
class OikosRunSetup:
    """Result of creating an Oikos run record (no execution started)."""

    run_id: int
    fiche_id: int
    thread_id: int
    trace_id: uuid.UUID


async def create_oikos_run(
    owner_id: int,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> OikosRunSetup:
    """Create a Run record and publish lifecycle events — no execution.

    Used by replay mode and any caller that needs a run_id without
    triggering real SurfaceOrchestrator execution.

    Returns:
        OikosRunSetup with run_id, fiche_id, thread_id, trace_id.
    """
    from zerg.database import db_session
    from zerg.events import EventType
    from zerg.events.event_bus import event_bus
    from zerg.models.enums import RunStatus
    from zerg.models.enums import RunTrigger

    trace_id = uuid.uuid4()

    with db_session() as db:
        service = OikosService(db)
        fiche = service.get_or_create_oikos_fiche(owner_id)
        thread = service.get_or_create_oikos_thread(owner_id, fiche)

        run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
            model=model or fiche.model,
            reasoning_effort=reasoning_effort,
            trace_id=trace_id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        setup = OikosRunSetup(
            run_id=run.id,
            fiche_id=fiche.id,
            thread_id=thread.id,
            trace_id=trace_id,
        )

    await event_bus.publish(
        EventType.RUN_CREATED,
        {
            "event_type": "run_created",
            "fiche_id": setup.fiche_id,
            "run_id": setup.run_id,
            "status": "running",
            "thread_id": setup.thread_id,
            "owner_id": owner_id,
        },
    )
    await event_bus.publish(
        EventType.RUN_UPDATED,
        {
            "event_type": "run_updated",
            "fiche_id": setup.fiche_id,
            "run_id": setup.run_id,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "thread_id": setup.thread_id,
            "owner_id": owner_id,
        },
    )

    return setup


async def invoke_oikos(
    owner_id: int,
    message: str,
    message_id: str,
    *,
    source: str = "web",
    model: str | None = None,
    reasoning_effort: str | None = None,
    surface_adapter: SurfaceAdapter | None = None,
    surface_payload: dict[str, Any] | None = None,
) -> int:
    """Start an Oikos execution without any transport coupling.

    Creates a Run record, publishes lifecycle events, and starts background
    execution via the SurfaceOrchestrator. Returns the run_id immediately.

    Any caller (HTTP endpoint, shepherd job, Telegram adapter, tests) can use
    this function. SSE streaming is a separate concern — callers can subscribe
    to the run's events via ``stream_run_events_live(run_id, owner_id)`` if
    they need real-time output.

    Args:
        owner_id: Longhouse owner ID for the run.
        message: Canonical text content of the inbound request.
        message_id: Stable caller-generated message ID.
        source: Fallback surface name used when no explicit adapter is provided.
        model: Optional model override for this turn.
        reasoning_effort: Optional reasoning effort override for this turn.
        surface_adapter: Optional explicit surface adapter for non-web callers.
        surface_payload: Optional adapter-specific raw payload merged with the
            canonical invoke fields before orchestration.
    """
    setup = await create_oikos_run(
        owner_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    run_id = setup.run_id
    trace_id = setup.trace_id

    effective_adapter = surface_adapter
    if effective_adapter is None:
        from zerg.surfaces.adapters.web import WebSurfaceAdapter

        effective_adapter = WebSurfaceAdapter(owner_id=owner_id)

    surface_id = str(getattr(effective_adapter, "surface_id", "") or source).strip() or source
    raw_input = dict(surface_payload or {})
    raw_input["owner_id"] = owner_id
    raw_input["message_id"] = message_id
    raw_input["run_id"] = run_id
    raw_input["trace_id"] = str(trace_id)
    raw_input.setdefault("message", message)
    raw_input.setdefault("conversation_id", f"{surface_id}:main")
    raw_input.setdefault("timeout", 600)
    raw_input.setdefault("return_on_deferred", False)
    if model is not None:
        raw_input.setdefault("model_override", model)
    if reasoning_effort is not None:
        raw_input.setdefault("reasoning_effort", reasoning_effort)

    logger.info(
        f"invoke_oikos: run {run_id} for user {owner_id}, surface={surface_id}, message: {message[:50]}...",
        extra={"tag": "OIKOS"},
    )

    async def _execute():
        from zerg.surfaces.base import SurfaceHandleStatus
        from zerg.surfaces.orchestrator import SurfaceOrchestrator

        try:
            orchestrator = SurfaceOrchestrator()
            handle_result = await orchestrator.handle_inbound(effective_adapter, raw_input)
            if handle_result.status != SurfaceHandleStatus.PROCESSED:
                raise RuntimeError(f"surface orchestration failed: {handle_result.status}")
        except Exception as e:
            logger.exception(f"invoke_oikos: background execution failed for run {run_id}: {e}")
            from zerg.events import EventType
            from zerg.events.event_bus import event_bus

            await event_bus.publish(
                EventType.ERROR,
                {
                    "event_type": "error",
                    "run_id": run_id,
                    "owner_id": owner_id,
                    "error": str(e),
                },
            )
        finally:
            # Mark run as FAILED if it's still RUNNING (crash safety)
            from zerg.database import db_session

            with db_session() as db:
                run_row = db.query(Run).filter(Run.id == run_id).first()
                if run_row and run_row.status == RunStatus.RUNNING:
                    run_row.status = RunStatus.FAILED
                    run_row.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    logger.warning(f"invoke_oikos: run {run_id} marked FAILED (still RUNNING after _execute)")

    asyncio.create_task(_execute())

    return run_id


__all__ = [
    "OikosService",
    "OikosRunResult",
    "OikosRunSetup",
    "create_oikos_run",
    "invoke_oikos",
    "OIKOS_THREAD_TYPE",
    "RECENT_COMMIS_HISTORY_LIMIT",
    "RECENT_COMMIS_HISTORY_MINUTES",
    "RECENT_COMMIS_CONTEXT_MARKER",
]
