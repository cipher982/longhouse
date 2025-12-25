"""Worker Job Processor Service.

This service manages the execution of worker jobs in the background.
It polls for queued worker jobs and executes them using the WorkerRunner.

Events emitted (for SSE streaming):
- WORKER_SPAWNED: When a job is picked up for processing
- WORKER_STARTED: When worker execution begins
- WORKER_COMPLETE: When worker finishes (success/failed/timeout)
- WORKER_SUMMARY_READY: When summary extraction completes
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timezone
from typing import Optional

from zerg.crud import crud
from zerg.database import db_session
from zerg.events import EventType
from zerg.events import event_bus
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.services.worker_runner import WorkerRunner

logger = logging.getLogger(__name__)


class WorkerJobProcessor:
    """Service to process queued worker jobs in the background."""

    def __init__(self):
        """Initialize the worker job processor."""
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 5  # Check every 5 seconds
        self._max_concurrent_jobs = 5  # Process up to 5 jobs concurrently

    async def start(self) -> None:
        """Start the worker job processor."""
        if self._running:
            logger.warning("Worker job processor already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._process_jobs_loop())
        logger.info("Worker job processor started")

    async def stop(self) -> None:
        """Stop the worker job processor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Worker job processor stopped")

    async def _process_jobs_loop(self) -> None:
        """Main processing loop for worker jobs."""
        while self._running:
            try:
                await self._process_pending_jobs()
            except Exception as e:
                logger.exception(f"Error in worker job processing loop: {e}")

            await asyncio.sleep(self._check_interval)

    async def _process_pending_jobs(self) -> None:
        """Process pending worker jobs."""
        # First, get job IDs with a short-lived session
        job_ids = []
        with db_session() as db:
            # Find queued jobs
            queued_jobs = (
                db.query(crud.WorkerJob)
                .filter(crud.WorkerJob.status == "queued")
                .order_by(crud.WorkerJob.created_at.asc())
                .limit(self._max_concurrent_jobs)
                .all()
            )

            if not queued_jobs:
                return

            # Extract just the IDs - the session will be released after this block
            job_ids = [job.id for job in queued_jobs]
            logger.info(f"Found {len(job_ids)} queued worker jobs")

        # Process jobs concurrently - each task gets its own session
        if job_ids:
            tasks = [asyncio.create_task(self._process_job_by_id(job_id)) for job_id in job_ids]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_job_by_id(self, job_id: int) -> None:
        """Process a single worker job by ID with its own database session."""
        # Each job gets its own session for thread safety
        with db_session() as db:
            job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
            if not job:
                logger.warning(f"Job {job_id} not found - may have been deleted")
                return

            # Check if job is still queued (another processor may have grabbed it)
            if job.status != "queued":
                logger.debug(f"Job {job_id} already being processed (status: {job.status})")
                return

            # Capture fields for event emission
            owner_id = job.owner_id
            task = job.task
            supervisor_run_id = job.supervisor_run_id  # For SSE correlation

            try:
                # Update job status to running
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                db.commit()

                logger.info(f"Starting worker job {job.id} for task: {job.task[:50]}...")

                # Emit WORKER_STARTED event (include run_id for SSE correlation)
                await event_bus.publish(
                    EventType.WORKER_STARTED,
                    {
                        "event_type": EventType.WORKER_STARTED,
                        "job_id": job.id,
                        "task": task[:100],
                        "owner_id": owner_id,
                        "run_id": supervisor_run_id,  # For SSE correlation
                    },
                )

                # Create worker runner
                artifact_store = WorkerArtifactStore()
                runner = WorkerRunner(artifact_store=artifact_store)

                # Execute the worker
                # Pass job.id for roundabout correlation, run_id for SSE tool events
                result = await runner.run_worker(
                    db=db,
                    task=job.task,
                    agent=None,  # Create temporary agent
                    agent_config={
                        "model": job.model,
                        "owner_id": job.owner_id,
                    },
                    job_id=job.id,
                    event_context={"run_id": supervisor_run_id},
                )

                # Update job with results
                job.worker_id = result.worker_id
                job.finished_at = datetime.now(timezone.utc)

                if result.status == "success":
                    job.status = "success"
                    logger.info(f"Worker job {job.id} completed successfully")
                else:
                    job.status = "failed"
                    job.error = result.error or "Unknown error"
                    logger.error(f"Worker job {job.id} failed: {job.error}")

                db.commit()

                # Emit WORKER_COMPLETE event (include run_id for SSE correlation)
                await event_bus.publish(
                    EventType.WORKER_COMPLETE,
                    {
                        "event_type": EventType.WORKER_COMPLETE,
                        "job_id": job.id,
                        "worker_id": result.worker_id,
                        "status": result.status,
                        "duration_ms": result.duration_ms,
                        "owner_id": owner_id,
                        "run_id": supervisor_run_id,  # For SSE correlation
                    },
                )

                # Durable runs v2.2 Phase 4: Trigger supervisor continuation for deferred runs
                # If this worker was spawned by a supervisor run, notify it of completion
                # The continuation endpoint will check if the run is actually DEFERRED
                if supervisor_run_id and result.status == "success":
                    await self._trigger_continuation(
                        supervisor_run_id=supervisor_run_id,
                        job_id=job.id,
                        worker_id=result.worker_id,
                        status=result.status,
                        result_summary=result.summary or result.result[:500] if result.result else "Task completed",
                    )

                # Emit WORKER_SUMMARY_READY if we have a summary
                if result.summary:
                    await event_bus.publish(
                        EventType.WORKER_SUMMARY_READY,
                        {
                            "event_type": EventType.WORKER_SUMMARY_READY,
                            "job_id": job.id,
                            "worker_id": result.worker_id,
                            "summary": result.summary,
                            "owner_id": owner_id,
                            "run_id": supervisor_run_id,  # For SSE correlation
                        },
                    )

            except Exception as e:
                logger.exception(f"Failed to process worker job {job.id}")

                # Update job with error
                try:
                    job.status = "failed"
                    job.error = str(e)
                    job.finished_at = datetime.now(timezone.utc)
                    db.commit()

                    # Emit error event (include run_id for SSE correlation)
                    await event_bus.publish(
                        EventType.WORKER_COMPLETE,
                        {
                            "event_type": EventType.WORKER_COMPLETE,
                            "job_id": job.id,
                            "status": "failed",
                            "error": str(e),
                            "owner_id": owner_id,
                            "run_id": supervisor_run_id,  # For SSE correlation
                        },
                    )
                except Exception as commit_error:
                    logger.error(f"Failed to commit error state for job {job.id}: {commit_error}")

    async def process_job_now(self, job_id: int) -> bool:
        """Process a specific job immediately (for testing/debugging).

        Args:
            job_id: The job ID to process

        Returns:
            True if job was found and processed, False otherwise
        """
        with db_session() as db:
            job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
            if not job:
                return False

            if job.status != "queued":
                logger.warning(f"Job {job_id} is not in queued state (status: {job.status})")
                return False

        # Process with its own session
        await self._process_job_by_id(job_id)
        return True

    async def _trigger_continuation(
        self,
        supervisor_run_id: int,
        job_id: int,
        worker_id: str,
        status: str,
        result_summary: str,
    ) -> None:
        """Trigger supervisor continuation for completed worker.

        Durable runs v2.2 Phase 4: When a worker completes, notify the supervisor
        so it can continue processing (if the run was deferred due to timeout).

        This is an internal webhook call - in a distributed setup this would be
        an HTTP POST to the continuation endpoint.

        Args:
            supervisor_run_id: The supervisor run that spawned this worker
            job_id: Completed worker job ID
            worker_id: Worker ID for artifact lookup
            status: Worker completion status
            result_summary: Summary of worker result
        """
        from zerg.services.supervisor_service import SupervisorService

        logger.info(f"Triggering continuation check for supervisor run {supervisor_run_id} " f"(job={job_id}, worker={worker_id})")

        try:
            # Use a fresh DB session for the continuation
            with db_session() as db:
                from zerg.models.enums import RunStatus
                from zerg.models.models import AgentRun

                # Check if run is actually deferred (quick check before expensive continuation)
                run = db.query(AgentRun).filter(AgentRun.id == supervisor_run_id).first()
                if not run:
                    logger.warning(f"Supervisor run {supervisor_run_id} not found for continuation")
                    return

                if run.status != RunStatus.DEFERRED:
                    logger.debug(
                        f"Supervisor run {supervisor_run_id} is {run.status.value}, not DEFERRED - "
                        "skipping continuation (supervisor may have completed normally)"
                    )
                    return

                # Trigger continuation
                supervisor_service = SupervisorService(db)
                result = await supervisor_service.run_continuation(
                    original_run_id=supervisor_run_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    result_summary=result_summary,
                )

                logger.info(f"Continuation run {result.run_id} completed for deferred run {supervisor_run_id}: " f"{result.status}")

        except Exception as e:
            logger.exception(f"Failed to trigger continuation for supervisor run {supervisor_run_id}: {e}")


# Singleton instance for application-wide use
worker_job_processor = WorkerJobProcessor()
