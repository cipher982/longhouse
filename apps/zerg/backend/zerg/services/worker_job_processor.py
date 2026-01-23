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
import os
from datetime import datetime
from datetime import timezone
from typing import Optional

from zerg.crud import crud
from zerg.database import db_session
from zerg.middleware.worker_db import current_worker_id
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.services.worker_runner import WorkerRunner

logger = logging.getLogger(__name__)

# E2E test mode detection
_is_e2e_mode = os.getenv("ENVIRONMENT") == "test:e2e"


class WorkerJobProcessor:
    """Service to process queued worker jobs in the background."""

    def __init__(self):
        """Initialize the worker job processor."""
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Interactive latency matters: workers are typically spawned from chat flows.
        # Keep polling reasonably tight so a queued job starts quickly.
        self._check_interval = 1  # seconds
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
        if _is_e2e_mode:
            # E2E mode: poll all test schemas for jobs
            await self._process_pending_jobs_e2e()
        else:
            # Normal mode: poll default schema
            await self._process_pending_jobs_default()

    async def _process_pending_jobs_default(self) -> None:
        """Process pending jobs from default schema (normal mode)."""
        job_ids = []
        with db_session() as db:
            queued_jobs = (
                db.query(crud.WorkerJob)
                .filter(crud.WorkerJob.status == "queued")
                .order_by(crud.WorkerJob.created_at.asc())
                .limit(self._max_concurrent_jobs)
                .all()
            )

            if not queued_jobs:
                return

            job_ids = [job.id for job in queued_jobs]
            logger.info(f"Found {len(job_ids)} queued worker jobs")

        if job_ids:
            tasks = [asyncio.create_task(self._process_job_by_id(job_id)) for job_id in job_ids]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_pending_jobs_e2e(self) -> None:
        """Process pending jobs from all E2E test schemas.

        In E2E mode, each Playwright worker gets its own Postgres schema.
        We need to poll all schemas to find queued jobs.
        """
        # Poll schemas 0-15 (matches Playwright worker count in test setup)
        # This is fast because empty schemas return immediately
        for worker_id in range(16):
            worker_id_str = str(worker_id)
            token = current_worker_id.set(worker_id_str)
            try:
                job_ids = []
                with db_session() as db:
                    queued_jobs = (
                        db.query(crud.WorkerJob)
                        .filter(crud.WorkerJob.status == "queued")
                        .order_by(crud.WorkerJob.created_at.asc())
                        .limit(self._max_concurrent_jobs)
                        .all()
                    )

                    if queued_jobs:
                        job_ids = [job.id for job in queued_jobs]
                        logger.info(f"Found {len(job_ids)} queued worker jobs in schema {worker_id}")

                if job_ids:
                    # Process jobs with the correct worker_id context
                    tasks = [asyncio.create_task(self._process_job_by_id_with_context(job_id, worker_id_str)) for job_id in job_ids]
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                current_worker_id.reset(token)

    async def _process_job_by_id_with_context(self, job_id: int, worker_id_str: str) -> None:
        """Process a job with the correct E2E schema context."""
        token = current_worker_id.set(worker_id_str)
        try:
            await self._process_job_by_id(job_id)
        finally:
            current_worker_id.reset(token)

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

            # Check for cloud execution mode
            job_config = job.config or {}
            execution_mode = job_config.get("execution_mode", "local")

            # Capture supervisor run ID for SSE correlation
            supervisor_run_id = job.supervisor_run_id

            try:
                # Update job status to running
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                db.commit()

                logger.info(f"Starting worker job {job.id} (mode={execution_mode}) for task: {job.task[:50]}...")

                if execution_mode == "cloud":
                    # Cloud execution: use agent-run subprocess with git workspace
                    await self._process_cloud_job(db, job, supervisor_run_id)
                else:
                    # Local execution: use standard WorkerRunner
                    await self._process_local_job(db, job, supervisor_run_id)

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

    async def _process_local_job(self, db, job, supervisor_run_id: Optional[int]) -> None:
        """Process a job using local WorkerRunner (standard path)."""
        # Create worker runner
        artifact_store = WorkerArtifactStore()
        runner = WorkerRunner(artifact_store=artifact_store)

        # Execute the worker
        # Pass job.id for roundabout correlation, run_id for SSE tool events
        # trace_id for end-to-end debugging (inherited from supervisor)
        result = await runner.run_worker(
            db=db,
            task=job.task,
            agent=None,  # Create temporary agent
            agent_config={
                "model": job.model,
                "reasoning_effort": job.reasoning_effort or "none",  # Inherit from supervisor
                "owner_id": job.owner_id,
            },
            job_id=job.id,
            event_context={
                "run_id": supervisor_run_id,
                "trace_id": str(job.trace_id) if job.trace_id else None,
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

    async def _process_cloud_job(self, db, job, supervisor_run_id: Optional[int]) -> None:
        """Process a job using cloud execution (agent-run subprocess with git workspace).

        This enables 24/7 execution on zerg-vps independent of laptop connectivity.
        The agent runs in a cloned git workspace and changes are captured as a diff.
        """
        from zerg.services.cloud_executor import CloudExecutor
        from zerg.services.workspace_manager import WorkspaceManager

        job_config = job.config or {}
        git_repo = job_config.get("git_repo")

        if not git_repo:
            job.status = "failed"
            job.error = "Cloud execution requires git_repo in job config"
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            logger.error(f"Worker job {job.id} failed: missing git_repo")
            return

        # Generate a unique worker_id for artifact storage
        import uuid

        worker_id = f"cloud-{job.id}-{uuid.uuid4().hex[:8]}"
        job.worker_id = worker_id
        db.commit()

        # Initialize managers
        workspace_manager = WorkspaceManager()
        cloud_executor = CloudExecutor()
        artifact_store = WorkerArtifactStore()

        workspace = None
        diff = ""
        try:
            # 1. Set up workspace
            base_branch = job_config.get("base_branch", "main")
            logger.info(f"Setting up cloud workspace for job {job.id}")
            workspace = await workspace_manager.setup(
                repo_url=git_repo,
                run_id=worker_id,
                base_branch=base_branch,
            )

            # 2. Create worker directory for artifacts
            artifact_store.create_worker(
                task=job.task,
                config={
                    "execution_mode": "cloud",
                    "git_repo": git_repo,
                    "workspace_path": str(workspace.path),
                },
                worker_id=worker_id,
            )
            artifact_store.start_worker(worker_id)

            # 3. Run agent in workspace
            logger.info(f"Running cloud agent for job {job.id} in {workspace.path}")
            result = await cloud_executor.run_agent(
                task=job.task,
                workspace_path=workspace.path,
                model=job.model,
            )

            # 4. Capture git diff (best-effort, don't fail job on diff errors)
            try:
                diff = await workspace_manager.capture_diff(workspace)
                if diff:
                    artifact_store.save_artifact(worker_id, "diff.patch", diff)
                    logger.info(f"Captured diff for job {job.id}: {len(diff)} bytes")
            except Exception as diff_error:
                logger.warning(f"Failed to capture diff for job {job.id}: {diff_error}")
                diff = ""  # Ensure diff is empty on error

            # 5. Save agent output as result
            artifact_store.save_result(worker_id, result.output or "(No output)")

            # 6. Update job status
            job.finished_at = datetime.now(timezone.utc)

            if result.status == "success":
                job.status = "success"
                artifact_store.complete_worker(worker_id, status="success")
                logger.info(f"Cloud worker job {job.id} completed successfully")
            else:
                job.status = "failed"
                job.error = result.error or "Unknown error"
                artifact_store.complete_worker(worker_id, status="failed", error=job.error)
                logger.error(f"Cloud worker job {job.id} failed: {job.error}")

            db.commit()

            # 7. Emit completion event for SSE (if supervisor run exists)
            if supervisor_run_id:
                from zerg.services.event_store import emit_run_event

                await emit_run_event(
                    db=db,
                    run_id=supervisor_run_id,
                    event_type="worker_complete",
                    payload={
                        "job_id": job.id,
                        "worker_id": worker_id,
                        "status": job.status,
                        "error": job.error if job.status == "failed" else None,
                        "duration_ms": result.duration_ms,
                        "owner_id": job.owner_id,
                        "execution_mode": "cloud",
                        "has_diff": bool(diff),
                        "trace_id": str(job.trace_id) if job.trace_id else None,
                    },
                )

                # Resume supervisor if waiting
                from zerg.services.worker_resume import resume_supervisor_with_worker_result

                summary = result.output[:500] if result.output else "(No output)"
                if diff:
                    summary += f"\n\n[Git diff captured: {len(diff)} bytes]"

                await resume_supervisor_with_worker_result(
                    db=db,
                    run_id=supervisor_run_id,
                    worker_result=summary,
                    job_id=job.id,
                )

        except Exception as e:
            logger.exception(f"Cloud execution failed for job {job.id}")

            # Update job with error
            job.status = "failed"
            job.error = str(e)
            job.finished_at = datetime.now(timezone.utc)

            if worker_id:
                try:
                    artifact_store.complete_worker(worker_id, status="failed", error=str(e))
                except Exception:
                    pass

            db.commit()

            # Emit failure event
            if supervisor_run_id:
                try:
                    from zerg.services.event_store import emit_run_event

                    await emit_run_event(
                        db=db,
                        run_id=supervisor_run_id,
                        event_type="worker_complete",
                        payload={
                            "job_id": job.id,
                            "worker_id": worker_id,
                            "status": "failed",
                            "error": str(e),
                            "owner_id": job.owner_id,
                            "execution_mode": "cloud",
                            "trace_id": str(job.trace_id) if job.trace_id else None,
                        },
                    )
                except Exception:
                    pass

        finally:
            # Cleanup workspace (optional - could defer for inspection)
            # For MVP, we keep the workspace for debugging
            pass

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
