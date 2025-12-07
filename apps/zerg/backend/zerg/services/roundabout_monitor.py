"""Roundabout monitoring for worker supervision.

The roundabout is a polling loop that provides real-time visibility into worker
execution without polluting the supervisor's long-lived thread context.

Like a highway interchange: the main thread temporarily enters a circular
monitoring pattern, checking exits (worker complete, early termination,
intervention needed) until the right one appears.

Phase 3 Implementation:
- Polling loop every 5 seconds
- Status aggregation from database and events
- Tool event subscription for activity tracking
- Returns structured result when worker completes
- Logs monitoring checks for audit trail

Phase 4 Implementation (Decision Handling):
- Heuristic-based decisions: wait/exit/cancel/peek
- Exit early when: worker completes, or final answer detected in output
- Cancel when: stuck > 60s with no progress
- Peek: returns structured payload for supervisor to drill down
- Future: LLM-based decisions for more sophisticated gating
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
from pathlib import Path
from typing import Any

from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)


# Configuration
ROUNDABOUT_CHECK_INTERVAL = 5  # seconds between status checks
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


def make_heuristic_decision(ctx: DecisionContext) -> tuple[RoundaboutDecision, str]:
    """Make a heuristic-based decision about what to do next in the roundabout.

    This is the v1 implementation using rules. Future versions may use LLM.

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

    # Priority 3: Cancel if stuck too long
    if ctx.is_stuck and ctx.stuck_seconds > ROUNDABOUT_CANCEL_STUCK_THRESHOLD:
        return (
            RoundaboutDecision.CANCEL,
            f"Operation stuck for {ctx.stuck_seconds:.0f}s (threshold: {ROUNDABOUT_CANCEL_STUCK_THRESHOLD}s)",
        )

    # Priority 4: Cancel if no progress for too many polls
    if ctx.polls_without_progress >= ROUNDABOUT_NO_PROGRESS_POLLS:
        return (
            RoundaboutDecision.CANCEL,
            f"No progress for {ctx.polls_without_progress} consecutive polls",
        )

    # Priority 5: Suggest peek if stuck but not cancel-worthy yet
    # (Future: could trigger LLM decision here)
    if ctx.is_stuck and ctx.stuck_seconds > ROUNDABOUT_STUCK_THRESHOLD:
        # For now, just flag as slow but continue waiting
        # A more sophisticated version might return PEEK
        logger.debug(
            f"Job {ctx.job_id} operation slow ({ctx.stuck_seconds:.0f}s) but not cancel-worthy yet"
        )

    # Default: continue waiting
    return RoundaboutDecision.WAIT, "Continuing to monitor"


class RoundaboutMonitor:
    """Monitors worker execution with periodic status checks.

    The monitor polls worker status every 5 seconds and tracks tool
    activity for visibility. When the worker completes (or times out),
    it returns a structured result.

    Usage:
        monitor = RoundaboutMonitor(db, job_id, owner_id)
        result = await monitor.wait_for_completion()
    """

    def __init__(
        self,
        db,
        job_id: int,
        owner_id: int,
        supervisor_run_id: int | None = None,
        timeout_seconds: float = ROUNDABOUT_HARD_TIMEOUT,
    ):
        self.db = db
        self.job_id = job_id
        self.owner_id = owner_id
        self.supervisor_run_id = supervisor_run_id
        self.timeout_seconds = timeout_seconds

        self._artifact_store = WorkerArtifactStore()
        self._tool_activities: list[ToolActivity] = []
        self._start_time: datetime | None = None
        self._check_count = 0
        self._event_subscription = None
        # Phase 4: Decision tracking
        self._last_activity_count = 0  # Tool count at last poll
        self._polls_without_progress = 0  # Consecutive polls with no new events
        self._last_tool_output: str | None = None  # Preview of last completed output
        self._task: str = ""  # Cached task description

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
        from zerg.events import EventType, event_bus
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
                        f"Roundabout monitor timeout for job {self.job_id} after {elapsed:.1f}s "
                        "(worker may still be running)"
                    )
                    # Get current job status to check if still running
                    self.db.expire_all()
                    job = (
                        self.db.query(WorkerJob)
                        .filter(WorkerJob.id == self.job_id, WorkerJob.owner_id == self.owner_id)
                        .first()
                    )
                    worker_running = job and job.status in ("queued", "running")
                    return self._create_timeout_result(
                        worker_id=job.worker_id if job else None,
                        worker_still_running=worker_running,
                    )

                # Get current job status
                self.db.expire_all()  # Refresh from database
                job = (
                    self.db.query(WorkerJob)
                    .filter(WorkerJob.id == self.job_id, WorkerJob.owner_id == self.owner_id)
                    .first()
                )

                if not job:
                    logger.error(f"Job {self.job_id} not found in roundabout")
                    return self._create_result("failed", error="Job not found")

                # Cache task for decision context
                self._task = job.task

                # Log monitoring check for audit
                await self._log_monitoring_check(job, elapsed)

                # Check if worker is done (priority check before heuristics)
                if job.status in ("success", "failed"):
                    logger.info(
                        f"Roundabout exit for job {self.job_id}: {job.status} after {elapsed:.1f}s"
                    )
                    return await self._create_completion_result(job)

                # Phase 4: Build decision context and make heuristic decision
                decision_ctx = self._build_decision_context(job, elapsed)
                decision, reason = make_heuristic_decision(decision_ctx)

                # Act on decision
                if decision == RoundaboutDecision.EXIT:
                    logger.info(
                        f"Roundabout early exit for job {self.job_id}: {reason}"
                    )
                    return await self._create_early_exit_result(job, reason)

                elif decision == RoundaboutDecision.CANCEL:
                    logger.warning(
                        f"Roundabout cancelling job {self.job_id}: {reason}"
                    )
                    return await self._create_cancel_result(job, reason)

                elif decision == RoundaboutDecision.PEEK:
                    logger.info(
                        f"Roundabout peek requested for job {self.job_id}: {reason}"
                    )
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

    async def _subscribe_to_tool_events(self) -> None:
        """Subscribe to tool events for this job."""
        from zerg.events import EventType, event_bus

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

        # Subscribe to all tool event types
        self._event_subscription = handle_tool_event
        event_bus.subscribe(EventType.WORKER_TOOL_STARTED, handle_tool_event)
        event_bus.subscribe(EventType.WORKER_TOOL_COMPLETED, handle_tool_event)
        event_bus.subscribe(EventType.WORKER_TOOL_FAILED, handle_tool_event)
        logger.debug(f"Subscribed to tool events for job {self.job_id}")

    async def _unsubscribe_from_tool_events(self) -> None:
        """Unsubscribe from tool events."""
        from zerg.events import EventType, event_bus

        if self._event_subscription:
            try:
                event_bus.unsubscribe(EventType.WORKER_TOOL_STARTED, self._event_subscription)
                event_bus.unsubscribe(EventType.WORKER_TOOL_COMPLETED, self._event_subscription)
                event_bus.unsubscribe(EventType.WORKER_TOOL_FAILED, self._event_subscription)
                logger.debug(f"Unsubscribed from tool events for job {self.job_id}")
            except Exception as e:
                logger.debug(f"Error unsubscribing from events: {e}")
            self._event_subscription = None

    def get_current_status(self) -> RoundaboutStatus:
        """Get current status snapshot (for future decision prompts)."""
        from zerg.models.models import WorkerJob

        elapsed = 0.0
        if self._start_time:
            elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        job = (
            self.db.query(WorkerJob)
            .filter(WorkerJob.id == self.job_id, WorkerJob.owner_id == self.owner_id)
            .first()
        )

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

        # Build activity summary
        completed_tools = [t for t in self._tool_activities if t.status == "completed"]
        tool_names = list({t.tool_name for t in self._tool_activities})

        activity_summary = {
            "tool_calls_total": len(self._tool_activities),
            "tool_calls_completed": len(completed_tools),
            "tools_used": tool_names,
            "monitoring_checks": self._check_count,
            "exit_reason": reason,
        }

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

        # Build activity summary
        tool_names = list({t.tool_name for t in self._tool_activities})

        activity_summary = {
            "tool_calls_total": len(self._tool_activities),
            "tools_used": tool_names,
            "monitoring_checks": self._check_count,
            "polls_without_progress": self._polls_without_progress,
            "cancel_reason": reason,
        }

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
            f"  read_worker_file('{self.job_id}', 'thread.jsonl')  # Full conversation\n"
            f"  read_worker_result('{self.job_id}')  # Final result (when complete)"
        )

        activity_summary = {
            "tool_calls_total": len(self._tool_activities),
            "monitoring_checks": self._check_count,
            "peek_reason": reason,
        }

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

    async def _create_completion_result(self, job) -> RoundaboutResult:
        """Create result when worker completes."""
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        result_text = None
        summary = None

        if job.worker_id and job.status == "success":
            try:
                result_text = self._artifact_store.get_worker_result(job.worker_id)
                metadata = self._artifact_store.get_worker_metadata(job.worker_id)
                summary = metadata.get("summary", result_text[:200] if result_text else None)
            except Exception as e:
                logger.warning(f"Failed to get worker result for {job.worker_id}: {e}")

        # Build activity summary
        completed_tools = [t for t in self._tool_activities if t.status == "completed"]
        failed_tools = [t for t in self._tool_activities if t.status == "failed"]
        tool_names = list({t.tool_name for t in self._tool_activities})

        activity_summary = {
            "tool_calls_total": len(self._tool_activities),
            "tool_calls_completed": len(completed_tools),
            "tool_calls_failed": len(failed_tools),
            "tools_used": tool_names,
            "monitoring_checks": self._check_count,
        }

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
            activity_summary={
                "tool_calls_total": len(self._tool_activities),
                "monitoring_checks": self._check_count,
            },
        )

    def _create_timeout_result(
        self, worker_id: str | None, worker_still_running: bool
    ) -> RoundaboutResult:
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
            activity_summary={
                "tool_calls_total": len(self._tool_activities),
                "monitoring_checks": self._check_count,
            },
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
    """
    lines = []

    if result.status == "complete":
        lines.append(f"Worker job {result.job_id} completed successfully.")
        lines.append(f"Duration: {result.duration_seconds:.1f}s")
        lines.append(f"Worker ID: {result.worker_id}")
        lines.append("")

        if result.summary:
            lines.append(f"Summary: {result.summary}")
            lines.append("")

        if result.result:
            # Truncate very long results
            if len(result.result) > 2000:
                lines.append("Result (truncated):")
                lines.append(result.result[:2000])
                lines.append("...")
                lines.append(
                    f"\nFull result available via read_worker_result({result.job_id})"
                )
            else:
                lines.append("Result:")
                lines.append(result.result)

    elif result.status == "failed":
        lines.append(f"Worker job {result.job_id} failed.")
        lines.append(f"Duration: {result.duration_seconds:.1f}s")
        if result.error:
            lines.append(f"Error: {result.error}")
        lines.append("")
        lines.append("Check worker artifacts for details:")
        lines.append(f"  read_worker_file('{result.job_id}', 'thread.jsonl')")

    elif result.status == "monitor_timeout":
        lines.append(f"Monitor timeout: stopped watching job {result.job_id} after {result.duration_seconds:.1f}s.")
        if result.worker_still_running:
            lines.append("NOTE: The worker is STILL RUNNING in the background.")
            lines.append("It may complete successfully - check status periodically:")
        else:
            lines.append("The worker appears to have stopped.")
        lines.append(f"  get_worker_metadata('{result.job_id}')")
        lines.append(f"  read_worker_result('{result.job_id}')  # when complete")

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
