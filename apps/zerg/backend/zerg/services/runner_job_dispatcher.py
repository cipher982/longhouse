"""Runner job dispatcher service.

Handles dispatching jobs to runners and tracking pending completions.
Manages concurrency control to ensure runners don't get overloaded.

IMPORTANT: The dispatcher uses thread-safe primitives (threading.Event)
instead of asyncio.Future because dispatch_job may be called from a
worker thread (via _run_coro_sync) while complete_job is called from
the main event loop's WebSocket handler. Using asyncio.Future would
cause the completion signal to be lost across event loop boundaries.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import Optional

from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.services.runner_connection_manager import get_runner_connection_manager

logger = logging.getLogger(__name__)


@dataclass
class PendingJob:
    """Thread-safe container for a pending job result."""

    event: threading.Event
    result: Optional[Dict[str, Any]] = None


class RunnerJobDispatcher:
    """Dispatches jobs to runners and tracks pending completions.

    Implements concurrency control to ensure each runner only processes
    one job at a time (v1 limitation).

    Uses thread-safe primitives for cross-event-loop signaling.
    """

    def __init__(self) -> None:
        """Initialize the dispatcher."""
        # Track pending jobs waiting for completion (thread-safe)
        # Key: job_id (UUID string), Value: PendingJob
        self._pending_jobs: Dict[str, PendingJob] = {}
        self._pending_lock = threading.Lock()

        # Track active job per runner for concurrency control
        # Key: runner_id (int), Value: job_id (UUID string)
        self._runner_active_jobs: Dict[int, str] = {}

    def can_accept_job(self, runner_id: int) -> bool:
        """Check if a runner can accept a new job.

        Args:
            runner_id: ID of the runner

        Returns:
            True if runner has no active jobs, False if busy
        """
        return runner_id not in self._runner_active_jobs

    def mark_job_active(self, runner_id: int, job_id: str) -> None:
        """Mark a job as active on a runner.

        Args:
            runner_id: ID of the runner
            job_id: UUID of the job
        """
        self._runner_active_jobs[runner_id] = job_id
        logger.debug(f"Marked job {job_id} as active on runner {runner_id}")

    def clear_active_job(self, runner_id: int) -> None:
        """Clear the active job for a runner.

        Args:
            runner_id: ID of the runner
        """
        job_id = self._runner_active_jobs.pop(runner_id, None)
        if job_id:
            logger.debug(f"Cleared active job {job_id} from runner {runner_id}")

    async def dispatch_job(
        self,
        db: Session,
        owner_id: int,
        runner_id: int,
        command: str,
        timeout_secs: int,
        worker_id: str | None = None,
        run_id: str | None = None,
    ) -> Dict[str, Any]:
        """Dispatch a job to a runner and wait for completion.

        Args:
            db: Database session
            owner_id: ID of the user owning the job
            runner_id: ID of the runner to execute on
            command: Shell command to execute
            timeout_secs: Maximum execution time in seconds
            worker_id: Optional worker ID for correlation
            run_id: Optional run ID for correlation

        Returns:
            Result dictionary with success/error envelope
        """
        # Check if runner can accept a job
        if not self.can_accept_job(runner_id):
            return {
                "ok": False,
                "error": {
                    "type": "execution_error",
                    "message": "Runner is busy with another job",
                },
            }

        # Get runner connection
        connection_manager = get_runner_connection_manager()
        if not connection_manager.is_online(owner_id, runner_id):
            return {
                "ok": False,
                "error": {
                    "type": "execution_error",
                    "message": "Runner is offline",
                },
            }

        # Create job record
        job = runner_crud.create_runner_job(
            db=db,
            owner_id=owner_id,
            runner_id=runner_id,
            command=command,
            timeout_secs=timeout_secs,
            worker_id=worker_id,
            run_id=run_id,
        )

        # Mark job as running
        runner_crud.update_job_started(db, job.id)

        # Mark runner as busy
        self.mark_job_active(runner_id, job.id)

        # Create thread-safe pending job for tracking completion
        # This allows cross-event-loop signaling between worker thread and main loop
        pending = PendingJob(event=threading.Event())
        with self._pending_lock:
            self._pending_jobs[job.id] = pending

        try:
            # Send exec_request to runner
            exec_request = {
                "type": "exec_request",
                "job_id": job.id,
                "command": command,
                "timeout_secs": timeout_secs,
            }

            success = await connection_manager.send_to_runner(
                owner_id=owner_id,
                runner_id=runner_id,
                message=exec_request,
            )

            if not success:
                # Failed to send message, clean up
                with self._pending_lock:
                    self._pending_jobs.pop(job.id, None)
                self.clear_active_job(runner_id)
                runner_crud.update_job_error(db, job.id, "Failed to send command to runner")
                return {
                    "ok": False,
                    "error": {
                        "type": "execution_error",
                        "message": "Failed to send command to runner",
                    },
                }

            # Wait for completion with timeout (thread-safe)
            # Add extra buffer to timeout to account for network latency
            wait_timeout = timeout_secs + 5

            # Use run_in_executor to wait on threading.Event without blocking event loop
            loop = asyncio.get_running_loop()
            completed = await loop.run_in_executor(None, lambda: pending.event.wait(timeout=wait_timeout))

            if not completed:
                # Job timed out waiting for response
                with self._pending_lock:
                    self._pending_jobs.pop(job.id, None)
                self.clear_active_job(runner_id)
                runner_crud.update_job_timeout(db, job.id)
                return {
                    "ok": False,
                    "error": {
                        "type": "execution_error",
                        "message": f"Job timed out after {timeout_secs} seconds",
                    },
                }

            # Event was set - return the result
            with self._pending_lock:
                self._pending_jobs.pop(job.id, None)
            return pending.result or {
                "ok": False,
                "error": {"type": "execution_error", "message": "No result received"},
            }

        except Exception as e:
            # Unexpected error
            with self._pending_lock:
                self._pending_jobs.pop(job.id, None)
            self.clear_active_job(runner_id)
            runner_crud.update_job_error(db, job.id, str(e))
            logger.exception(f"Error dispatching job {job.id}")
            return {
                "ok": False,
                "error": {
                    "type": "execution_error",
                    "message": f"Unexpected error: {str(e)}",
                },
            }

    def complete_job(
        self,
        job_id: str,
        result: Dict[str, Any],
        runner_id: int | None = None,
    ) -> None:
        """Complete a pending job with a result.

        Called when exec_done or exec_error is received from the runner.
        Thread-safe - can be called from any thread or event loop.

        Args:
            job_id: UUID of the job
            result: Result dictionary to return from dispatch_job
            runner_id: Optional runner ID to clear active job tracking
        """
        with self._pending_lock:
            pending = self._pending_jobs.get(job_id)

        if pending:
            pending.result = result
            pending.event.set()  # Signal completion
            logger.debug(f"Completed job {job_id}")
        else:
            logger.warning(f"complete_job called for unknown job {job_id}")

        # Clear active job tracking
        if runner_id is not None:
            self.clear_active_job(runner_id)


# Global singleton instance
_dispatcher_instance: Optional[RunnerJobDispatcher] = None


def get_runner_job_dispatcher() -> RunnerJobDispatcher:
    """Get the global runner job dispatcher instance.

    Returns:
        RunnerJobDispatcher singleton
    """
    global _dispatcher_instance
    if _dispatcher_instance is None:
        _dispatcher_instance = RunnerJobDispatcher()
    return _dispatcher_instance
