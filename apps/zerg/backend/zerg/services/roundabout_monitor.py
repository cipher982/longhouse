"""Roundabout monitoring for worker supervision.

v2.0 Philosophy: Trust the AI, Remove Scaffolding
-------------------------------------------------
The roundabout is a polling loop that provides real-time visibility into worker
execution without polluting the supervisor's long-lived thread context.

Like glancing at a second monitor: the supervisor polls worker status periodically,
and the LLM interprets what it sees to decide the next action.

This is v2.0's "trust the AI" approach:
- Polling (supervisor checking status) = GOOD (like glancing at second monitor)
- LLM interprets status and decides = GOOD (trust the AI's judgment)
- Hard guardrails (timeouts, rate limits) = GOOD (safety boundaries, not heuristics)
- Heuristic decision engine = DEPRECATED (pre-programs LLM decisions)

Implementation:
- Polling loop every 5 seconds (supervisor checking status)
- Status aggregation from database and events
- Tool event subscription for activity tracking
- LLM interprets status and decides: wait, exit, cancel, or peek
- Hard guardrails: poll interval, max calls budget, timeout
- Returns structured result when worker completes
- Logs monitoring checks for audit trail

Decision modes (v2.0 default: LLM):
- LLM (v2.0 default): LLM interprets status and decides
- Heuristic (DEPRECATED): Pre-programmed decision rules (v1.0 approach)
- Hybrid (DEPRECATED): Heuristic first, then LLM for ambiguous cases
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from zerg.models.models import WorkerJob

from zerg.config import get_settings
from zerg.services.llm_decider import DEFAULT_DECISION_MODE
from zerg.services.llm_decider import DEFAULT_LLM_MAX_CALLS
from zerg.services.llm_decider import DEFAULT_LLM_MODEL
from zerg.services.llm_decider import DEFAULT_LLM_POLL_INTERVAL
from zerg.services.llm_decider import DecisionMode
from zerg.services.llm_decider import LLMDeciderStats
from zerg.services.llm_decider import decide_roundabout_action
from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)


# Configuration
# Chat UX is latency-sensitive; tighter polling makes "wait=True" feel snappier.
ROUNDABOUT_CHECK_INTERVAL = 1  # seconds between status checks
ROUNDABOUT_HARD_TIMEOUT = 300  # seconds (5 minutes) max time in roundabout
ROUNDABOUT_STUCK_THRESHOLD = 30  # seconds - flag operation as slow
ROUNDABOUT_ACTIVITY_LOG_MAX = 20  # max entries to track
ROUNDABOUT_CANCEL_STUCK_THRESHOLD = 60  # seconds - auto-cancel if stuck this long
ROUNDABOUT_NO_PROGRESS_POLLS = 6  # consecutive polls with no new events before cancel

# Patterns that suggest worker has a final answer (case-insensitive)
FINAL_ANSWER_PATTERNS = [
    r"Result:",
    r"Summary:",
    r"Completed successfully",
    r"Task complete",
    r"Done\.",
]


class RoundaboutDecision(Enum):
    """Decision options for the roundabout monitoring loop."""

    WAIT = "wait"  # Continue monitoring (default)
    EXIT = "exit"  # Saw enough, return early with current findings
    CANCEL = "cancel"  # Something wrong, abort worker
    PEEK = "peek"  # Need more details, return pointer to drill down


@dataclass
class DecisionContext:
    """Context for making roundabout decisions."""

    job_id: int
    worker_id: str | None
    task: str
    status: str  # queued, running, success, failed
    elapsed_seconds: float
    tool_activities: list["ToolActivity"]
    current_operation: "ToolActivity | None"
    is_stuck: bool
    stuck_seconds: float  # how long current operation has been running
    polls_without_progress: int  # consecutive polls with no new tool events
    last_tool_output: str | None  # preview of last completed tool output


@dataclass
class ToolActivity:
    """Record of a tool call during worker execution."""

    tool_name: str
    status: str  # "started", "completed", "failed"
    timestamp: datetime
    duration_ms: int | None = None
    args_preview: str | None = None
    error: str | None = None


@dataclass
class ToolIndexEntry:
    """Execution metadata for a single tool call in the tool index.

    This is NOT domain parsing - just execution metadata (exit codes, sizes, durations).
    The tool index provides a compact summary of what tools ran and their outcomes.
    """

    sequence: int
    tool_name: str
    exit_code: int | None = None
    duration_ms: int | None = None
    output_bytes: int = 0
    failed: bool = False


@dataclass
class RoundaboutStatus:
    """Current status of a worker in the roundabout."""

    job_id: int
    worker_id: str | None
    task: str
    status: str  # queued, running, success, failed
    elapsed_seconds: float
    tool_calls: list[ToolActivity] = field(default_factory=list)
    current_operation: ToolActivity | None = None
    is_stuck: bool = False
    error: str | None = None


@dataclass
class RoundaboutResult:
    """Final result from the roundabout when exiting."""

    status: str  # "complete", "early_exit", "cancelled", "monitor_timeout", "failed", "peek"
    job_id: int
    worker_id: str | None
    duration_seconds: float
    worker_still_running: bool = False  # True if monitor timed out but worker continues
    result: str | None = None
    summary: str | None = None
    error: str | None = None
    activity_summary: dict[str, Any] = field(default_factory=dict)
    decision: RoundaboutDecision | None = None  # The decision that triggered exit
    drill_down_hint: str | None = None  # For peek: what to read next
    tool_index: list[ToolIndexEntry] = field(default_factory=list)  # Execution metadata for tool calls
    run_id: int | None = None  # Supervisor run ID for evidence correlation


def make_heuristic_decision(ctx: DecisionContext) -> tuple[RoundaboutDecision, str]:
    """Make a heuristic-based decision about what to do next in the roundabout.

    DEPRECATED (v2.0): This is the v1.0 approach using pre-programmed rules.
    v2.0 default is LLM mode - let the AI interpret status and decide.

    Kept for backwards compatibility only. Use DecisionMode.LLM instead.

    Args:
        ctx: Decision context with current state

    Returns:
        Tuple of (decision, reason)
    """
    # Priority 1: Worker completed - exit immediately
    if ctx.status in ("success", "failed"):
        return RoundaboutDecision.EXIT, f"Worker status changed to {ctx.status}"

    # Priority 2: Check for final answer patterns in last tool output
    if ctx.last_tool_output:
        for pattern in FINAL_ANSWER_PATTERNS:
            if re.search(pattern, ctx.last_tool_output, re.IGNORECASE):
                return (
                    RoundaboutDecision.EXIT,
                    f"Final answer pattern detected: {pattern}",
                )

    # Priority 3: Warn (not cancel) if stuck too long
    # v2.2: Timeouts stop waiting, not working. Let hard timeout be the safety net.
    if ctx.is_stuck and ctx.stuck_seconds > ROUNDABOUT_CANCEL_STUCK_THRESHOLD:
        logger.warning(f"Job {ctx.job_id}: operation stuck for {ctx.stuck_seconds:.0f}s - " "continuing (hard timeout is safety net)")
        # Don't cancel - just log. LLM may be thinking or waiting for SSH response.

    # Priority 4: Warn (not cancel) if no progress for too many polls
    # v2.2: Timeouts stop waiting, not working. Let hard timeout be the safety net.
    if ctx.polls_without_progress >= ROUNDABOUT_NO_PROGRESS_POLLS:
        logger.warning(
            f"Job {ctx.job_id}: {ctx.polls_without_progress} polls without progress - " "continuing (hard timeout is safety net)"
        )
        # Don't cancel - just log. LLM may be reasoning or waiting for external service.

    # Priority 5: Suggest peek if stuck but not cancel-worthy yet
    # (Future: could trigger LLM decision here)
    if ctx.is_stuck and ctx.stuck_seconds > ROUNDABOUT_STUCK_THRESHOLD:
        # For now, just flag as slow but continue waiting
        # A more sophisticated version might return PEEK
        logger.debug(f"Job {ctx.job_id} operation slow ({ctx.stuck_seconds:.0f}s) but not cancel-worthy yet")

    # Default: continue waiting
    return RoundaboutDecision.WAIT, "Continuing to monitor"


class RoundaboutMonitor:
    """Monitors worker execution with periodic status checks.

    v2.0 default: LLM interprets status and decides (trust the AI).

    The monitor polls worker status every 5 seconds ("glancing at a second monitor")
    and the LLM interprets what it sees to decide the next action. Hard guardrails
    (poll interval, max calls, timeout) provide safety boundaries without pre-programming
    the LLM's decisions.

    When the worker completes (or times out), it returns a structured result.

    Decision modes (v2.0 default: LLM):
    - LLM (v2.0 default): Let the AI interpret status and decide
    - Heuristic (DEPRECATED): Pre-programmed rules-based decisions (v1.0 approach)
    - Hybrid (DEPRECATED): Heuristic first, LLM for ambiguous cases

    Usage:
        # v2.0 default: LLM mode (trust the AI)
        monitor = RoundaboutMonitor(db, job_id, owner_id)
        result = await monitor.wait_for_completion()

        # Override to use deprecated heuristic mode (not recommended)
        monitor = RoundaboutMonitor(db, job_id, owner_id, decision_mode=DecisionMode.HEURISTIC)
        result = await monitor.wait_for_completion()
    """

    def __init__(
        self,
        db,
        job_id: int,
        owner_id: int,
        supervisor_run_id: int | None = None,
        timeout_seconds: float = ROUNDABOUT_HARD_TIMEOUT,
        decision_mode: DecisionMode = DEFAULT_DECISION_MODE,
        llm_poll_interval: int = DEFAULT_LLM_POLL_INTERVAL,
        llm_max_calls: int = DEFAULT_LLM_MAX_CALLS,
        llm_timeout_seconds: float | None = None,
        llm_model: str = DEFAULT_LLM_MODEL,
    ):
        self.db = db
        self.job_id = job_id
        self.owner_id = owner_id
        self.supervisor_run_id = supervisor_run_id
        self.timeout_seconds = timeout_seconds

        # Phase 5: LLM decision configuration (v2.0 default: LLM mode)
        self.decision_mode = decision_mode

        # Warn if using deprecated heuristic mode
        if decision_mode in (DecisionMode.HEURISTIC, DecisionMode.HYBRID):
            import warnings

            warnings.warn(
                f"DecisionMode.{decision_mode.name} is deprecated. "
                "v2.0 uses DecisionMode.LLM (trust the AI to interpret status). "
                "Heuristic decision engines pre-program the LLM's decisions, which goes against "
                "the v2.0 philosophy of trusting the AI.",
                DeprecationWarning,
                stacklevel=2,
            )
        self.llm_poll_interval = llm_poll_interval
        self.llm_max_calls = llm_max_calls
        self.llm_timeout_seconds = llm_timeout_seconds
        self.llm_model = llm_model

        self._artifact_store = WorkerArtifactStore()
        self._tool_activities: list[ToolActivity] = []
        self._start_time: datetime | None = None
        self._check_count = 0
        self._event_subscription = None
        self._heartbeat_subscription = None  # Phase 6: Heartbeat event subscription
        # Phase 4: Decision tracking
        self._last_activity_count = 0  # Tool count at last poll
        self._polls_without_progress = 0  # Consecutive polls with no new events
        self._last_tool_output: str | None = None  # Preview of last completed output
        self._task: str = ""  # Cached task description
        # Phase 5: LLM decision tracking
        self._llm_stats = LLMDeciderStats()
        self._llm_calls_made = 0  # Track calls for budget enforcement

    async def _make_decision(self, ctx: DecisionContext) -> tuple[RoundaboutDecision, str]:
        """Make a decision using the configured mode.

        This method integrates heuristic and LLM decision making based on
        the configured decision_mode.

        Args:
            ctx: Decision context with current state

        Returns:
            Tuple of (decision, reason)
        """
        # Mode: heuristic only
        if self.decision_mode == DecisionMode.HEURISTIC:
            return make_heuristic_decision(ctx)

        # Mode: LLM only
        if self.decision_mode == DecisionMode.LLM:
            return await self._make_llm_decision(ctx)

        # Mode: hybrid - heuristic first, LLM for ambiguous cases
        heuristic_decision, heuristic_reason = make_heuristic_decision(ctx)

        # If heuristic says anything other than WAIT, use it
        if heuristic_decision != RoundaboutDecision.WAIT:
            return heuristic_decision, f"[heuristic] {heuristic_reason}"

        # Heuristic says WAIT - optionally consult LLM
        llm_decision, llm_reason = await self._make_llm_decision(ctx)

        # If LLM says something actionable, use it
        if llm_decision != RoundaboutDecision.WAIT:
            return llm_decision, f"[llm] {llm_reason}"

        # Both say wait
        return RoundaboutDecision.WAIT, f"[hybrid] {heuristic_reason}"

    async def _make_llm_decision(self, ctx: DecisionContext) -> tuple[RoundaboutDecision, str]:
        """Make a decision using the LLM decider.

        Respects budget and interval constraints. Falls back to WAIT on any issue.

        Args:
            ctx: Decision context

        Returns:
            Tuple of (decision, reason)
        """
        # Check budget
        if self._llm_calls_made >= self.llm_max_calls:
            self._llm_stats.record_skip("budget")
            logger.debug(f"Job {self.job_id}: LLM budget exhausted ({self._llm_calls_made}/{self.llm_max_calls})")
            return RoundaboutDecision.WAIT, "LLM budget exhausted, continuing to monitor"

        # Check interval (only call every N polls)
        if self._check_count % self.llm_poll_interval != 0:
            self._llm_stats.record_skip("interval")
            logger.debug(f"Job {self.job_id}: Skipping LLM (poll {self._check_count}, interval {self.llm_poll_interval})")
            return RoundaboutDecision.WAIT, "Continuing to monitor"

        # Make LLM call
        try:
            action, rationale, result = await decide_roundabout_action(
                ctx,
                model=self.llm_model,
                timeout_seconds=self.llm_timeout_seconds,
            )
            self._llm_calls_made += 1
            self._llm_stats.record_call(result)

            # Map string action to enum
            action_map = {
                "wait": RoundaboutDecision.WAIT,
                "exit": RoundaboutDecision.EXIT,
                "cancel": RoundaboutDecision.CANCEL,
                "peek": RoundaboutDecision.PEEK,
            }
            decision = action_map.get(action, RoundaboutDecision.WAIT)

            return decision, rationale

        except Exception as e:
            logger.warning(f"Job {self.job_id}: LLM decision error: {e}")
            return RoundaboutDecision.WAIT, f"LLM error ({e}), continuing to monitor"

    async def wait_for_completion(self) -> RoundaboutResult:
        """Enter the roundabout and wait for worker completion.

        Polls worker status every 5 seconds until:
        - Worker completes (success or failure)
        - Heuristic decision triggers early exit or cancel
        - Hard timeout reached (returns monitor_timeout, worker may continue)
        - Error occurs

        Returns:
            RoundaboutResult with final status and result
        """
        from zerg.models.models import WorkerJob

        self._start_time = datetime.now(timezone.utc)
        logger.info(f"Entering roundabout for job {self.job_id}")

        # Subscribe to tool events for this job
        await self._subscribe_to_tool_events()

        try:
            while True:
                self._check_count += 1
                elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

                # Check timeout - monitor timeout, not job failure
                if elapsed > self.timeout_seconds:
                    logger.warning(
                        f"Roundabout monitor timeout for job {self.job_id} after {elapsed:.1f}s " "(worker may still be running)"
                    )
                    # Get current job status to check if still running
                    self.db.expire_all()
                    job = self.db.query(WorkerJob).filter(WorkerJob.id == self.job_id, WorkerJob.owner_id == self.owner_id).first()
                    worker_running = job and job.status in ("queued", "running")
                    return self._create_timeout_result(
                        worker_id=job.worker_id if job else None,
                        worker_still_running=worker_running,
                    )

                # Get current job status
                self.db.expire_all()  # Refresh from database
                job = self.db.query(WorkerJob).filter(WorkerJob.id == self.job_id, WorkerJob.owner_id == self.owner_id).first()

                if not job:
                    logger.error(f"Job {self.job_id} not found in roundabout")
                    return self._create_result("failed", error="Job not found")

                # Evaluation Mode Helper:
                # In testing/eval mode, the background worker processor is disabled.
                # If we're waiting for a job that is still 'queued', we drain it
                # synchronously here so the supervisor can proceed with findings.
                if get_settings().testing and job.status == "queued":
                    logger.info(f"Eval Mode: Draining job {self.job_id} synchronously in roundabout (immediate)")
                    await self._drain_job_synchronously(job, timeout_seconds=self.timeout_seconds)
                    # Refresh job after draining
                    self.db.expire_all()
                    job = self.db.query(WorkerJob).filter(WorkerJob.id == self.job_id, WorkerJob.owner_id == self.owner_id).first()

                # Cache task for decision context
                self._task = job.task

                # Log monitoring check for audit
                await self._log_monitoring_check(job, elapsed)

                # Check if worker is done (priority check before heuristics)
                if job.status in ("success", "failed"):
                    logger.info(f"Roundabout exit for job {self.job_id}: {job.status} after {elapsed:.1f}s")
                    return await self._create_completion_result(job)

                # Phase 4/5: Build decision context and make decision
                decision_ctx = self._build_decision_context(job, elapsed)
                decision, reason = await self._make_decision(decision_ctx)

                # Act on decision
                if decision == RoundaboutDecision.EXIT:
                    logger.info(f"Roundabout early exit for job {self.job_id}: {reason}")
                    return await self._create_early_exit_result(job, reason)

                elif decision == RoundaboutDecision.CANCEL:
                    logger.warning(f"Roundabout cancelling job {self.job_id}: {reason}")
                    return await self._create_cancel_result(job, reason)

                elif decision == RoundaboutDecision.PEEK:
                    logger.info(f"Roundabout peek requested for job {self.job_id}: {reason}")
                    return self._create_peek_result(job, reason)

                # decision == WAIT: continue monitoring

                # Update progress tracking
                current_activity_count = len(self._tool_activities)
                if current_activity_count > self._last_activity_count:
                    self._polls_without_progress = 0
                    self._last_activity_count = current_activity_count
                else:
                    self._polls_without_progress += 1

                # Log progress periodically
                if self._check_count % 4 == 0:  # Every 20 seconds
                    logger.info(
                        f"Roundabout check #{self._check_count} for job {self.job_id}: "
                        f"status={job.status}, elapsed={elapsed:.1f}s, tools={len(self._tool_activities)}, "
                        f"no_progress_polls={self._polls_without_progress}"
                    )

                # Wait before next check
                await asyncio.sleep(ROUNDABOUT_CHECK_INTERVAL)
        finally:
            # Unsubscribe from events
            await self._unsubscribe_from_tool_events()

    async def _drain_job_synchronously(self, job: "WorkerJob", timeout_seconds: float = 60.0) -> None:
        """Execute a queued worker job synchronously (eval-only)."""
        from zerg.services.worker_runner import WorkerRunner
        from zerg.utils.time import utc_now_naive

        # Update status to running
        job.status = "running"
        job.started_at = utc_now_naive()
        self.db.commit()

        artifact_store = WorkerArtifactStore()
        runner = WorkerRunner(artifact_store=artifact_store)

        try:
            result = await runner.run_worker(
                db=self.db,
                task=job.task,
                agent=None,
                agent_config={"model": job.model, "owner_id": job.owner_id},
                timeout=int(timeout_seconds),
                event_context={"run_id": self.supervisor_run_id},
                job_id=job.id,
            )

            # Ensure job is still attached to session
            self.db.refresh(job)

            job.worker_id = result.worker_id
            job.finished_at = utc_now_naive()

            if result.status == "success":
                job.status = "success"
            else:
                job.status = "failed"
                job.error = result.error or "Unknown error"

            self.db.commit()
        except Exception as e:
            self.db.rollback()
            self.db.refresh(job)
            job.status = "failed"
            job.error = str(e)
            job.finished_at = utc_now_naive()
            self.db.commit()

    async def _subscribe_to_tool_events(self) -> None:
        """Subscribe to tool events for this job."""
        from zerg.events import EventType
        from zerg.events import event_bus

        async def handle_tool_event(payload: dict[str, Any]) -> None:
            """Handle incoming tool events."""
            # Filter to events for this job
            event_job_id = payload.get("job_id")
            if event_job_id != self.job_id:
                return

            event_type = payload.get("event_type")
            if event_type:
                self.record_tool_activity(
                    event_type.value if hasattr(event_type, "value") else str(event_type),
                    payload,
                )

        async def handle_heartbeat_event(payload: dict[str, Any]) -> None:
            """Handle incoming heartbeat events (Phase 6)."""
            # Filter to events for this job
            event_job_id = payload.get("job_id")
            if event_job_id != self.job_id:
                return

            # Reset no-progress counter - the worker is actively reasoning
            self._polls_without_progress = 0
            logger.debug(f"Job {self.job_id}: Heartbeat received, reset no-progress counter")

        # Subscribe to all tool event types
        self._event_subscription = handle_tool_event
        event_bus.subscribe(EventType.WORKER_TOOL_STARTED, handle_tool_event)
        event_bus.subscribe(EventType.WORKER_TOOL_COMPLETED, handle_tool_event)
        event_bus.subscribe(EventType.WORKER_TOOL_FAILED, handle_tool_event)

        # Phase 6: Subscribe to heartbeat events to track LLM reasoning progress
        self._heartbeat_subscription = handle_heartbeat_event
        event_bus.subscribe(EventType.WORKER_HEARTBEAT, handle_heartbeat_event)

        logger.debug(f"Subscribed to tool and heartbeat events for job {self.job_id}")

    async def _unsubscribe_from_tool_events(self) -> None:
        """Unsubscribe from tool events."""
        from zerg.events import EventType
        from zerg.events import event_bus

        if self._event_subscription:
            try:
                event_bus.unsubscribe(EventType.WORKER_TOOL_STARTED, self._event_subscription)
                event_bus.unsubscribe(EventType.WORKER_TOOL_COMPLETED, self._event_subscription)
                event_bus.unsubscribe(EventType.WORKER_TOOL_FAILED, self._event_subscription)
                logger.debug(f"Unsubscribed from tool events for job {self.job_id}")
            except Exception as e:
                logger.debug(f"Error unsubscribing from events: {e}")
            self._event_subscription = None

        # Phase 6: Unsubscribe from heartbeat events
        if self._heartbeat_subscription:
            try:
                event_bus.unsubscribe(EventType.WORKER_HEARTBEAT, self._heartbeat_subscription)
                logger.debug(f"Unsubscribed from heartbeat events for job {self.job_id}")
            except Exception as e:
                logger.debug(f"Error unsubscribing from heartbeat events: {e}")
            self._heartbeat_subscription = None

    def get_current_status(self) -> RoundaboutStatus:
        """Get current status snapshot (for future decision prompts)."""
        from zerg.models.models import WorkerJob

        elapsed = 0.0
        if self._start_time:
            elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        job = self.db.query(WorkerJob).filter(WorkerJob.id == self.job_id, WorkerJob.owner_id == self.owner_id).first()

        if not job:
            return RoundaboutStatus(
                job_id=self.job_id,
                worker_id=None,
                task="Unknown",
                status="unknown",
                elapsed_seconds=elapsed,
                error="Job not found",
            )

        # Check if current operation is stuck
        current_op = None
        is_stuck = False
        if self._tool_activities:
            last = self._tool_activities[-1]
            if last.status == "started":
                current_op = last
                op_elapsed = (datetime.now(timezone.utc) - last.timestamp).total_seconds()
                is_stuck = op_elapsed > ROUNDABOUT_STUCK_THRESHOLD

        return RoundaboutStatus(
            job_id=self.job_id,
            worker_id=job.worker_id,
            task=job.task,
            status=job.status,
            elapsed_seconds=elapsed,
            tool_calls=self._tool_activities[-ROUNDABOUT_ACTIVITY_LOG_MAX:],
            current_operation=current_op,
            is_stuck=is_stuck,
            error=job.error,
        )

    def _build_decision_context(self, job, elapsed: float) -> DecisionContext:
        """Build context for heuristic decision making."""
        # Check if current operation is stuck
        current_op = None
        is_stuck = False
        stuck_seconds = 0.0

        if self._tool_activities:
            last = self._tool_activities[-1]
            if last.status == "started":
                current_op = last
                stuck_seconds = (datetime.now(timezone.utc) - last.timestamp).total_seconds()
                is_stuck = stuck_seconds > ROUNDABOUT_STUCK_THRESHOLD

        return DecisionContext(
            job_id=self.job_id,
            worker_id=job.worker_id,
            task=job.task,
            status=job.status,
            elapsed_seconds=elapsed,
            tool_activities=self._tool_activities[-ROUNDABOUT_ACTIVITY_LOG_MAX:],
            current_operation=current_op,
            is_stuck=is_stuck,
            stuck_seconds=stuck_seconds,
            polls_without_progress=self._polls_without_progress,
            last_tool_output=self._last_tool_output,
        )

    def _build_activity_summary(self, **extra) -> dict[str, Any]:
        """Build activity summary with common fields and LLM stats.

        Args:
            **extra: Additional fields to include in the summary

        Returns:
            Dictionary with activity summary
        """
        completed_tools = [t for t in self._tool_activities if t.status == "completed"]
        failed_tools = [t for t in self._tool_activities if t.status == "failed"]
        tool_names = list({t.tool_name for t in self._tool_activities})

        summary = {
            "tool_calls_total": len(self._tool_activities),
            "tool_calls_completed": len(completed_tools),
            "tool_calls_failed": len(failed_tools),
            "tools_used": tool_names,
            "monitoring_checks": self._check_count,
        }

        # Add LLM stats (includes skips even if no calls were made)
        llm_stats = self._llm_stats.to_dict()
        if llm_stats:
            summary.update(llm_stats)

        # Add decision mode
        summary["decision_mode"] = self.decision_mode.value

        # Add extra fields
        summary.update(extra)

        return summary

    async def _create_early_exit_result(self, job, reason: str) -> RoundaboutResult:
        """Create result for early exit (answer detected in output)."""
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        # Try to get partial result if worker has produced any output
        partial_result = None
        if job.worker_id:
            try:
                partial_result = self._artifact_store.get_worker_result(job.worker_id)
            except Exception:
                pass  # Worker may not have result yet

        activity_summary = self._build_activity_summary(exit_reason=reason)

        return RoundaboutResult(
            status="early_exit",
            job_id=self.job_id,
            worker_id=job.worker_id,
            duration_seconds=elapsed,
            worker_still_running=job.status in ("queued", "running"),
            result=partial_result,
            summary=f"Early exit: {reason}",
            activity_summary=activity_summary,
            decision=RoundaboutDecision.EXIT,
        )

    async def _create_cancel_result(self, job, reason: str) -> RoundaboutResult:
        """Create result for cancel (stuck/no progress)."""
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        # Mark job as cancelled in database (soft cancel)
        try:
            job.status = "cancelled"
            job.error = f"Cancelled by roundabout: {reason}"
            self.db.commit()
            logger.info(f"Marked job {self.job_id} as cancelled")
        except Exception as e:
            logger.warning(f"Failed to mark job {self.job_id} as cancelled: {e}")

        activity_summary = self._build_activity_summary(
            polls_without_progress=self._polls_without_progress,
            cancel_reason=reason,
        )

        return RoundaboutResult(
            status="cancelled",
            job_id=self.job_id,
            worker_id=job.worker_id,
            duration_seconds=elapsed,
            worker_still_running=False,  # We've marked it cancelled
            error=reason,
            activity_summary=activity_summary,
            decision=RoundaboutDecision.CANCEL,
        )

    def _create_peek_result(self, job, reason: str) -> RoundaboutResult:
        """Create result for peek (need more details)."""
        elapsed = 0.0
        if self._start_time:
            elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        # Build drill-down hint
        drill_down_hint = (
            f"For more details, use:\n"
            f"  read_commis_file('{self.job_id}', 'thread.jsonl')  # Full conversation\n"
            f"  read_commis_result('{self.job_id}')  # Final result (when complete)"
        )

        activity_summary = self._build_activity_summary(peek_reason=reason)

        return RoundaboutResult(
            status="peek",
            job_id=self.job_id,
            worker_id=job.worker_id,
            duration_seconds=elapsed,
            worker_still_running=job.status in ("queued", "running"),
            summary=f"Peek requested: {reason}",
            activity_summary=activity_summary,
            decision=RoundaboutDecision.PEEK,
            drill_down_hint=drill_down_hint,
        )

    def record_tool_activity(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record tool activity from events."""
        timestamp = datetime.now(timezone.utc)

        # Normalize event type to lower-case string for robust matching
        event_str = event_type.value if hasattr(event_type, "value") else str(event_type)
        event_str = event_str.lower()

        if "started" in event_str:
            activity = ToolActivity(
                tool_name=payload.get("tool_name", "unknown"),
                status="started",
                timestamp=timestamp,
                args_preview=payload.get("args_preview"),
            )
            self._tool_activities.append(activity)
            logger.debug(f"Recorded tool start: {activity.tool_name}")

        elif "completed" in event_str or "failed" in event_str:
            # Find matching started activity and update it
            tool_name = payload.get("tool_name", "unknown")
            is_failed = "failed" in event_str
            for activity in reversed(self._tool_activities):
                if activity.tool_name == tool_name and activity.status == "started":
                    activity.status = "failed" if is_failed else "completed"
                    activity.duration_ms = payload.get("duration_ms")
                    if is_failed:
                        activity.error = payload.get("error")
                    logger.debug(f"Recorded tool {activity.status}: {tool_name}")
                    break

            # Phase 4: Capture last tool output for heuristic decisions
            # The output preview helps detect final answers
            if not is_failed:
                output_preview = payload.get("result_preview") or payload.get("output_preview")
                if output_preview:
                    self._last_tool_output = output_preview[:500]  # Cap at 500 chars

    def _build_tool_index(self, worker_id: str) -> list[ToolIndexEntry]:
        """Build tool index from worker artifacts.

        This reads the tool_calls directory and extracts execution metadata
        (exit codes, sizes, durations) for each tool call.

        Parameters
        ----------
        worker_id
            Worker ID to read artifacts from

        Returns
        -------
        list[ToolIndexEntry]
            Tool execution metadata entries
        """
        try:
            worker_dir = self._artifact_store._get_worker_dir(worker_id)
            tool_calls_dir = worker_dir / "tool_calls"

            if not tool_calls_dir.exists():
                return []

            tool_index = []

            for filepath in sorted(tool_calls_dir.glob("*.txt")):
                # Parse filename: "001_ssh_exec.txt" -> sequence=1, tool_name="ssh_exec"
                filename = filepath.name
                try:
                    seq_str, tool_name_ext = filename.split("_", 1)
                    sequence = int(seq_str)
                    tool_name = tool_name_ext.replace(".txt", "")
                except ValueError:
                    logger.warning(f"Skipping malformed tool output filename: {filename}")
                    continue

                # Get file size
                output_bytes = filepath.stat().st_size

                # Try to extract exit code and failure status from tool output
                exit_code, failed = self._extract_tool_metadata(filepath)

                # Try to get duration from activity log (best effort)
                duration_ms = self._get_tool_duration(tool_name, sequence)

                tool_index.append(
                    ToolIndexEntry(
                        sequence=sequence,
                        tool_name=tool_name,
                        exit_code=exit_code,
                        duration_ms=duration_ms,
                        output_bytes=output_bytes,
                        failed=failed,
                    )
                )

            return tool_index

        except Exception as e:
            logger.warning(f"Failed to build tool index for worker {worker_id}: {e}")
            return []

    def _extract_tool_metadata(self, filepath) -> tuple[int | None, bool]:
        """Extract exit code and failure status from tool output file.

        Tool outputs are JSON envelopes: {"ok": bool, "data": {...}, "error": ...}
        For ssh_exec, data contains: {"exit_code": N, "stdout": ..., "stderr": ...}

        Parameters
        ----------
        filepath
            Path to tool output file

        Returns
        -------
        tuple[int | None, bool]
            (exit_code, failed) - exit_code is None if not extractable
        """
        try:
            content = filepath.read_text()
            data = json.loads(content)

            # Check if this is an error envelope
            if not data.get("ok", True):
                return (None, True)

            # Try to extract exit_code from data
            tool_data = data.get("data", {})
            if isinstance(tool_data, dict):
                exit_code = tool_data.get("exit_code")
                if exit_code is not None:
                    # Non-zero exit code means command failed
                    return (exit_code, exit_code != 0)

            return (None, False)

        except (json.JSONDecodeError, OSError):
            # Can't parse - assume not failed
            return (None, False)

    def _get_tool_duration(self, tool_name: str, sequence: int) -> int | None:
        """Get tool duration from activity log (best effort).

        Parameters
        ----------
        tool_name
            Name of the tool
        sequence
            Sequence number of the tool call

        Returns
        -------
        int | None
            Duration in milliseconds if found
        """
        # Try to find matching activity with duration
        for activity in self._tool_activities:
            if activity.tool_name == tool_name and activity.duration_ms is not None:
                return activity.duration_ms

        return None

    async def _create_completion_result(self, job) -> RoundaboutResult:
        """Create result when worker completes."""
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        result_text = None
        summary = None
        tool_index = []

        if job.worker_id and job.status == "success":
            try:
                result_text = self._artifact_store.get_worker_result(job.worker_id)
                metadata = self._artifact_store.get_worker_metadata(job.worker_id)
                summary = metadata.get("summary", result_text[:200] if result_text else None)

                # Build tool index from artifacts
                tool_index = self._build_tool_index(job.worker_id)
            except Exception as e:
                logger.warning(f"Failed to get worker result for {job.worker_id}: {e}")

        activity_summary = self._build_activity_summary()

        return RoundaboutResult(
            status="complete" if job.status == "success" else "failed",
            job_id=self.job_id,
            worker_id=job.worker_id,
            duration_seconds=elapsed,
            worker_still_running=False,
            result=result_text,
            summary=summary,
            error=job.error if job.status == "failed" else None,
            activity_summary=activity_summary,
            tool_index=tool_index,
            run_id=self.supervisor_run_id,
        )

    def _create_result(self, status: str, error: str | None = None) -> RoundaboutResult:
        """Create result for non-completion exits (cancel, etc)."""
        elapsed = 0.0
        if self._start_time:
            elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        return RoundaboutResult(
            status=status,
            job_id=self.job_id,
            worker_id=None,
            duration_seconds=elapsed,
            worker_still_running=False,
            error=error,
            activity_summary=self._build_activity_summary(),
        )

    def _create_timeout_result(self, worker_id: str | None, worker_still_running: bool) -> RoundaboutResult:
        """Create result for monitor timeout (distinct from job failure)."""
        elapsed = 0.0
        if self._start_time:
            elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        return RoundaboutResult(
            status="monitor_timeout",
            job_id=self.job_id,
            worker_id=worker_id,
            duration_seconds=elapsed,
            worker_still_running=worker_still_running,
            error=f"Monitor timeout after {elapsed:.0f}s",
            activity_summary=self._build_activity_summary(),
        )

    async def _log_monitoring_check(self, job, elapsed: float) -> None:
        """Log monitoring check for audit trail."""
        if not job.worker_id:
            return  # No worker directory yet

        try:
            monitoring_dir = self._artifact_store._get_worker_dir(job.worker_id) / "monitoring"
            monitoring_dir.mkdir(parents=True, exist_ok=True)

            check_file = monitoring_dir / f"check_{int(elapsed):04d}s.json"
            check_data = {
                "check_number": self._check_count,
                "elapsed_seconds": elapsed,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_status": job.status,
                "tool_activities": len(self._tool_activities),
                "tool_names": [t.tool_name for t in self._tool_activities[-5:]],
            }

            check_file.write_text(json.dumps(check_data, indent=2))
        except Exception as e:
            logger.debug(f"Failed to log monitoring check: {e}")


def format_roundabout_result(result: RoundaboutResult) -> str:
    """Format roundabout result for supervisor thread.

    This is what gets persisted to the supervisor's conversation history.
    Returns a compact payload with:
    - Tool index (execution metadata)
    - Summary (worker's prose, may be empty/garbage)
    - Evidence marker for LLM wrapper expansion

    The evidence marker format is: [EVIDENCE:run_id=48,job_id=123,worker_id=abc-123]
    """
    lines = []

    if result.status == "complete":
        lines.append(f"Worker job {result.job_id} completed successfully.")
        lines.append(f"Duration: {result.duration_seconds:.1f}s | Worker ID: {result.worker_id}")
        lines.append("")

        # Tool Index (execution metadata, not domain parsing)
        if result.tool_index:
            lines.append("Tool Index:")
            for entry in result.tool_index:
                # Build status indicator
                if entry.failed:
                    status = "FAILED"
                elif entry.exit_code == 0:
                    status = f"exit={entry.exit_code}"
                elif entry.exit_code is not None:
                    status = f"exit={entry.exit_code}"
                else:
                    status = "ok"

                # Build duration indicator
                duration_str = f"{entry.duration_ms}ms" if entry.duration_ms is not None else "?ms"

                # Format: "  1. ssh_exec [exit=0, 234ms, 1847B]"
                lines.append(f"  {entry.sequence}. {entry.tool_name} [{status}, {duration_str}, {entry.output_bytes}B]")
            lines.append("")

        # Summary (worker's prose, truncated to 500 chars)
        if result.summary:
            summary_truncated = result.summary[:500] if len(result.summary) > 500 else result.summary
            lines.append(f"Summary: {summary_truncated}")
            lines.append("")
        elif result.result:
            # Fallback to result if no summary
            result_truncated = result.result[:500] if len(result.result) > 500 else result.result
            lines.append(f"Summary: {result_truncated}")
            lines.append("")

        # Evidence marker for LLM wrapper expansion
        if result.run_id is not None and result.worker_id is not None:
            lines.append(f"[EVIDENCE:run_id={result.run_id},job_id={result.job_id},worker_id={result.worker_id}]")

    elif result.status == "failed":
        lines.append(f"Worker job {result.job_id} failed.")
        lines.append(f"Duration: {result.duration_seconds:.1f}s")
        if result.error:
            lines.append(f"Error: {result.error}")
        lines.append("")
        lines.append("Check worker artifacts for details:")
        lines.append(f"  read_worker_file('{result.job_id}', 'thread.jsonl')")
        lines.append("")

        # Evidence marker for LLM wrapper expansion (even for failures - useful tool output may exist)
        if result.run_id is not None and result.worker_id is not None:
            lines.append(f"[EVIDENCE:run_id={result.run_id},job_id={result.job_id},worker_id={result.worker_id}]")

    elif result.status == "monitor_timeout":
        lines.append(f"Monitor timeout: stopped watching job {result.job_id} after {result.duration_seconds:.1f}s.")
        if result.worker_still_running:
            lines.append("NOTE: The worker is STILL RUNNING in the background.")
            lines.append("It may complete successfully - check status periodically:")
        else:
            lines.append("The worker appears to have stopped.")
        lines.append(f"  get_commis_metadata('{result.job_id}')")
        lines.append(f"  read_commis_result('{result.job_id}')  # when complete")
        lines.append("")

        # Evidence marker for LLM wrapper expansion (even for timeouts - partial output may be useful)
        if result.run_id is not None and result.worker_id is not None:
            lines.append(f"[EVIDENCE:run_id={result.run_id},job_id={result.job_id},worker_id={result.worker_id}]")

    elif result.status == "early_exit":
        lines.append(f"Exited monitoring of worker job {result.job_id} early.")
        lines.append(f"Elapsed: {result.duration_seconds:.1f}s")
        if result.summary:
            lines.append(f"Partial findings: {result.summary}")

    elif result.status == "cancelled":
        lines.append(f"Worker job {result.job_id} was cancelled.")
        lines.append(f"Elapsed: {result.duration_seconds:.1f}s")
        if result.error:
            lines.append(f"Reason: {result.error}")
        if result.worker_still_running:
            lines.append("NOTE: Worker may still be running - cancellation is best-effort.")

    elif result.status == "peek":
        lines.append(f"Peek requested for worker job {result.job_id}.")
        lines.append(f"Elapsed: {result.duration_seconds:.1f}s")
        if result.summary:
            lines.append(f"Reason: {result.summary}")
        if result.worker_still_running:
            lines.append("Worker is still running in background.")
        lines.append("")
        if result.drill_down_hint:
            lines.append(result.drill_down_hint)

    # Add activity summary
    if result.activity_summary:
        lines.append("")
        lines.append("Activity summary:")
        for key, value in result.activity_summary.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)
