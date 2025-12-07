"""Roundabout monitoring for worker supervision.

The roundabout is a polling loop that provides real-time visibility into worker
execution without polluting the supervisor's long-lived thread context.

Like a highway interchange: the main thread temporarily enters a circular
monitoring pattern, checking exits (worker complete, early termination,
intervention needed) until the right one appears.

Phase 3 Implementation:
- Polling loop every 5 seconds
- Status aggregation from database and events
- Returns structured result when worker completes
- Logs monitoring checks for audit trail

Future phases will add:
- Decision handling (wait/exit/cancel/peek)
- Ephemeral prompts to supervisor LLM
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)


# Configuration
ROUNDABOUT_CHECK_INTERVAL = 5  # seconds between status checks
ROUNDABOUT_HARD_TIMEOUT = 300  # seconds (5 minutes) max time in roundabout
ROUNDABOUT_STUCK_THRESHOLD = 30  # seconds - flag operation as slow
ROUNDABOUT_ACTIVITY_LOG_MAX = 20  # max entries to track


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

    status: str  # "complete", "early_exit", "cancelled", "timeout", "failed"
    job_id: int
    worker_id: str | None
    duration_seconds: float
    result: str | None = None
    summary: str | None = None
    error: str | None = None
    activity_summary: dict[str, Any] = field(default_factory=dict)


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

    async def wait_for_completion(self) -> RoundaboutResult:
        """Enter the roundabout and wait for worker completion.

        Polls worker status every 5 seconds until:
        - Worker completes (success or failure)
        - Hard timeout reached
        - Error occurs

        Returns:
            RoundaboutResult with final status and result
        """
        from zerg.models.models import WorkerJob

        self._start_time = datetime.now(timezone.utc)
        logger.info(f"Entering roundabout for job {self.job_id}")

        while True:
            self._check_count += 1
            elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

            # Check timeout
            if elapsed > self.timeout_seconds:
                logger.warning(f"Roundabout timeout for job {self.job_id} after {elapsed:.1f}s")
                return self._create_result("timeout", error="Hard timeout reached")

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

            # Log monitoring check for audit
            await self._log_monitoring_check(job, elapsed)

            # Check if worker is done
            if job.status in ("success", "failed"):
                logger.info(
                    f"Roundabout exit for job {self.job_id}: {job.status} after {elapsed:.1f}s"
                )
                return await self._create_completion_result(job)

            # Log progress
            if self._check_count % 4 == 0:  # Every 20 seconds
                logger.info(
                    f"Roundabout check #{self._check_count} for job {self.job_id}: "
                    f"status={job.status}, elapsed={elapsed:.1f}s"
                )

            # Wait before next check
            await asyncio.sleep(ROUNDABOUT_CHECK_INTERVAL)

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

    def record_tool_activity(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record tool activity from SSE events."""
        timestamp = datetime.now(timezone.utc)

        if event_type == "WORKER_TOOL_STARTED":
            activity = ToolActivity(
                tool_name=payload.get("tool_name", "unknown"),
                status="started",
                timestamp=timestamp,
                args_preview=payload.get("args_preview"),
            )
            self._tool_activities.append(activity)

        elif event_type in ("WORKER_TOOL_COMPLETED", "WORKER_TOOL_FAILED"):
            # Find matching started activity and update it
            tool_name = payload.get("tool_name", "unknown")
            for activity in reversed(self._tool_activities):
                if activity.tool_name == tool_name and activity.status == "started":
                    activity.status = "completed" if event_type == "WORKER_TOOL_COMPLETED" else "failed"
                    activity.duration_ms = payload.get("duration_ms")
                    if event_type == "WORKER_TOOL_FAILED":
                        activity.error = payload.get("error")
                    break

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
            result=result_text,
            summary=summary,
            error=job.error if job.status == "failed" else None,
            activity_summary=activity_summary,
        )

    def _create_result(self, status: str, error: str | None = None) -> RoundaboutResult:
        """Create result for non-completion exits (timeout, cancel, etc)."""
        elapsed = 0.0
        if self._start_time:
            elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        return RoundaboutResult(
            status=status,
            job_id=self.job_id,
            worker_id=None,
            duration_seconds=elapsed,
            error=error,
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

    elif result.status == "timeout":
        lines.append(f"Worker job {result.job_id} timed out after {result.duration_seconds:.1f}s.")
        lines.append("The worker may still be running in the background.")
        lines.append("Check status with: get_worker_metadata('{result.job_id}')")

    elif result.status == "early_exit":
        lines.append(f"Exited monitoring of worker job {result.job_id} early.")
        lines.append(f"Elapsed: {result.duration_seconds:.1f}s")
        if result.summary:
            lines.append(f"Partial findings: {result.summary}")

    elif result.status == "cancelled":
        lines.append(f"Worker job {result.job_id} was cancelled.")
        lines.append(f"Elapsed: {result.duration_seconds:.1f}s")

    # Add activity summary
    if result.activity_summary:
        lines.append("")
        lines.append("Activity summary:")
        for key, value in result.activity_summary.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)
