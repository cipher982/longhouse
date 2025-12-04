"""Worker Job Processor Service.

This service manages the execution of worker jobs in the background.
It polls for queued worker jobs and executes them using the WorkerRunner.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timezone
from typing import Optional

from zerg.crud import crud
from zerg.database import db_session
from zerg.services.worker_runner import WorkerRunner
from zerg.services.worker_artifact_store import WorkerArtifactStore

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

            try:
                # Update job status to running
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                db.commit()

                logger.info(f"Starting worker job {job.id} for task: {job.task[:50]}...")

                # Create worker runner
                artifact_store = WorkerArtifactStore()
                runner = WorkerRunner(artifact_store=artifact_store)

                # Execute the worker
                result = await runner.run_worker(
                    db=db,
                    task=job.task,
                    agent=None,  # Create temporary agent
                    agent_config={
                        "model": job.model,
                        "owner_id": job.owner_id,
                    },
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

            except Exception as e:
                logger.exception(f"Failed to process worker job {job.id}")

                # Update job with error
                try:
                    job.status = "failed"
                    job.error = str(e)
                    job.finished_at = datetime.now(timezone.utc)
                    db.commit()
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


# Singleton instance for application-wide use
worker_job_processor = WorkerJobProcessor()
