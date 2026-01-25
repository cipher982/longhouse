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
        """Process pending jobs from default schema (normal mode).

        Uses atomic UPDATE ... RETURNING to prevent race conditions where
        multiple processors could claim the same job.
        """
        job_ids = []
        with db_session() as db:
            # Atomic job pickup: UPDATE status to 'running' WHERE status='queued'
            # This prevents race conditions where multiple processors grab the same job
            from sqlalchemy import text

            result = db.execute(
                text("""
                    UPDATE worker_jobs
                    SET status = 'running', started_at = NOW()
                    WHERE id IN (
                        SELECT id FROM worker_jobs
                        WHERE status = 'queued'
                        ORDER BY created_at ASC
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id
                """),
                {"limit": self._max_concurrent_jobs},
            )
            job_ids = [row[0] for row in result.fetchall()]
            db.commit()

            if not job_ids:
                return

            logger.info(f"Claimed {len(job_ids)} queued worker jobs atomically")

        if job_ids:
            tasks = [asyncio.create_task(self._process_job_by_id(job_id, already_claimed=True)) for job_id in job_ids]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_pending_jobs_e2e(self) -> None:
        """Process pending jobs from all E2E test schemas.

        In E2E mode, each Playwright worker gets its own Postgres schema.
        We need to poll all schemas to find queued jobs.

        Uses atomic UPDATE ... RETURNING to prevent race conditions.
        """
        from sqlalchemy import text

        # Poll schemas 0-15 (matches Playwright worker count in test setup)
        # This is fast because empty schemas return immediately
        for worker_id in range(16):
            worker_id_str = str(worker_id)
            token = current_worker_id.set(worker_id_str)
            try:
                job_ids = []
                with db_session() as db:
                    # Atomic job pickup for E2E mode
                    result = db.execute(
                        text("""
                            UPDATE worker_jobs
                            SET status = 'running', started_at = NOW()
                            WHERE id IN (
                                SELECT id FROM worker_jobs
                                WHERE status = 'queued'
                                ORDER BY created_at ASC
                                LIMIT :limit
                                FOR UPDATE SKIP LOCKED
                            )
                            RETURNING id
                        """),
                        {"limit": self._max_concurrent_jobs},
                    )
                    job_ids = [row[0] for row in result.fetchall()]
                    db.commit()

                    if job_ids:
                        logger.info(f"Claimed {len(job_ids)} queued worker jobs in schema {worker_id}")

                if job_ids:
                    # Process jobs with the correct worker_id context
                    tasks = [
                        asyncio.create_task(self._process_job_by_id_with_context(job_id, worker_id_str, already_claimed=True))
                        for job_id in job_ids
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                current_worker_id.reset(token)

    async def _process_job_by_id_with_context(self, job_id: int, worker_id_str: str, *, already_claimed: bool = False) -> None:
        """Process a job with the correct E2E schema context."""
        token = current_worker_id.set(worker_id_str)
        try:
            await self._process_job_by_id(job_id, already_claimed=already_claimed)
        finally:
            current_worker_id.reset(token)

    async def _process_job_by_id(self, job_id: int, *, already_claimed: bool = False) -> None:
        """Process a single worker job by ID with its own database session.

        Parameters
        ----------
        job_id
            The job ID to process
        already_claimed
            If True, the job was already atomically claimed (status set to 'running'),
            so skip the status check and update. This prevents race conditions.
        """
        # First, fetch job data and determine execution mode
        # This session is short-lived - we extract data and close before execution
        execution_mode = "local"
        supervisor_run_id = None
        job_task_preview = ""

        with db_session() as db:
            job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
            if not job:
                logger.warning(f"Job {job_id} not found - may have been deleted")
                return

            if not already_claimed:
                # Check if job is still queued (another processor may have grabbed it)
                if job.status != "queued":
                    logger.debug(f"Job {job_id} already being processed (status: {job.status})")
                    return

            # Check for workspace execution mode (accepts "cloud" for backward compat)
            job_config = job.config or {}
            execution_mode = job_config.get("execution_mode", "standard")

            # Capture supervisor run ID for SSE correlation
            supervisor_run_id = job.supervisor_run_id
            job_task_preview = job.task[:50] if job.task else ""

            if not already_claimed:
                # Update job status to running (only if not already claimed atomically)
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                db.commit()

        # Session is now closed - execute outside of any db session context
        logger.info(f"Starting worker job {job_id} (mode={execution_mode}) for task: {job_task_preview}...")

        if execution_mode in ("cloud", "workspace"):
            # Workspace execution: manages its own short-lived sessions
            # No db session passed - _process_workspace_job opens/closes sessions as needed
            await self._process_workspace_job(job_id, supervisor_run_id)
        else:
            # Standard execution: use WorkerRunner with its own session
            # Handles "local", "standard", or None
            await self._process_standard_job(job_id, supervisor_run_id)

    async def _process_standard_job(self, job_id: int, supervisor_run_id: Optional[int]) -> None:
        """Process a job using standard WorkerRunner (in-process execution).

        Opens its own short-lived db sessions as needed.
        """
        # Fetch job data in a short-lived session
        with db_session() as db:
            job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
            if not job:
                logger.error(f"Job {job_id} not found when starting local execution")
                return

            # Extract all needed data
            job_task = job.task
            job_model = job.model
            job_reasoning_effort = job.reasoning_effort or "none"
            job_owner_id = job.owner_id
            job_trace_id = str(job.trace_id) if job.trace_id else None

        # Create worker runner (outside db session)
        # Artifact store is best-effort - if init fails, run without it
        artifact_store = None
        try:
            artifact_store = WorkerArtifactStore()
        except Exception as e:
            logger.warning(f"Failed to initialize artifact store for job {job_id}, continuing without it: {e}")

        try:
            # WorkerRunner may try to create artifact store if None was passed
            # If that fails, the main try block will catch it and mark job failed
            runner = WorkerRunner(artifact_store=artifact_store)
            # Execute the worker - WorkerRunner manages its own db sessions internally
            # Pass job_id for roundabout correlation, run_id for SSE tool events
            # trace_id for end-to-end debugging (inherited from supervisor)
            with db_session() as exec_db:
                result = await runner.run_worker(
                    db=exec_db,
                    task=job_task,
                    agent=None,  # Create temporary agent
                    agent_config={
                        "model": job_model,
                        "reasoning_effort": job_reasoning_effort,
                        "owner_id": job_owner_id,
                    },
                    job_id=job_id,
                    event_context={
                        "run_id": supervisor_run_id,
                        "trace_id": job_trace_id,
                    },
                )

            # Update job with results in a new short-lived session
            with db_session() as update_db:
                update_job = update_db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
                if not update_job:
                    logger.error(f"Job {job_id} not found when updating final status")
                    return

                update_job.worker_id = result.worker_id
                update_job.finished_at = datetime.now(timezone.utc)

                if result.status == "success":
                    update_job.status = "success"
                    logger.info(f"Worker job {job_id} completed successfully")
                else:
                    update_job.status = "failed"
                    update_job.error = result.error or "Unknown error"
                    logger.error(f"Worker job {job_id} failed: {update_job.error}")

                update_db.commit()

        except Exception as e:
            logger.exception(f"Failed to process local worker job {job_id}")
            # Update job with error in a new session
            with db_session() as error_db:
                error_job = error_db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
                if error_job:
                    error_job.status = "failed"
                    error_job.error = str(e)
                    error_job.finished_at = datetime.now(timezone.utc)
                    error_db.commit()

    async def _process_workspace_job(self, job_id: int, supervisor_run_id: Optional[int]) -> None:
        """Process a job using workspace execution (hatch subprocess with git workspace).

        This enables 24/7 execution on zerg-vps independent of laptop connectivity.
        The agent runs in a cloned git workspace and changes are captured as a diff.

        This method manages its own short-lived db sessions to avoid exhausting
        the connection pool during long-running workspace execution.
        """
        import uuid

        from zerg.services.cloud_executor import CloudExecutor
        from zerg.services.workspace_manager import WorkspaceManager

        # Extract all needed data from job in a short-lived session
        with db_session() as db:
            job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
            if not job:
                logger.error(f"Job {job_id} not found when starting workspace execution")
                return

            job_task = job.task
            job_model = job.model
            job_owner_id = job.owner_id
            job_trace_id = str(job.trace_id) if job.trace_id else None
            job_config = job.config or {}
            git_repo = job_config.get("git_repo")
            resume_session_id = job_config.get("resume_session_id")

            if not git_repo:
                job.status = "failed"
                job.error = "Workspace execution requires git_repo in job config"
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
                logger.error(f"Worker job {job_id} failed: missing git_repo")
                return

            # Generate a unique worker_id for artifact storage
            worker_id = f"ws-{job_id}-{uuid.uuid4().hex[:8]}"
            job.worker_id = worker_id
            base_branch = job_config.get("base_branch", "main")

            # Commit worker_id before long-running execution
            db.commit()
        # Session is now closed

        # Initialize managers (stateless, no DB needed)
        workspace_manager = WorkspaceManager()
        # Support E2E_HATCH_PATH env var for mock hatch in E2E tests
        hatch_path = os.environ.get("E2E_HATCH_PATH")
        cloud_executor = CloudExecutor(hatch_path=hatch_path)

        # Artifact store is best-effort - if init fails, continue without it
        artifact_store = None
        try:
            artifact_store = WorkerArtifactStore()
        except Exception as e:
            logger.warning(f"Failed to initialize artifact store for workspace job {job_id}, continuing without it: {e}")

        workspace = None
        diff = ""
        result = None
        execution_error = None

        try:
            # 1. Set up workspace (no DB needed)
            logger.info(f"Setting up workspace for job {job_id}")
            workspace = await workspace_manager.setup(
                repo_url=git_repo,
                run_id=worker_id,
                base_branch=base_branch,
            )

            # 2. Create worker directory for artifacts (best-effort, don't fail job)
            if artifact_store:
                try:
                    artifact_store.create_worker(
                        task=job_task,
                        config={
                            "execution_mode": "workspace",
                            "git_repo": git_repo,
                            "workspace_path": str(workspace.path),
                        },
                        worker_id=worker_id,
                    )
                    artifact_store.start_worker(worker_id)
                except Exception as artifact_error:
                    logger.warning(f"Failed to set up artifact store for job {job_id}, continuing without it: {artifact_error}")
                    artifact_store = None  # Disable further artifact operations

            # 3. Prepare session for resume if resume_session_id provided
            prepared_resume_id = None
            if resume_session_id:
                try:
                    from zerg.services.session_continuity import prepare_session_for_resume

                    prepared_resume_id = await prepare_session_for_resume(
                        session_id=resume_session_id,
                        workspace_path=workspace.path,
                    )
                    logger.info(f"Prepared session {resume_session_id} for resume as {prepared_resume_id}")
                except Exception as resume_error:
                    logger.warning(f"Failed to prepare session for resume: {resume_error}")
                    # Continue without resume - treat as new session

            # 4. Run agent in workspace (LONG-RUNNING - no DB session held!)
            logger.info(f"Running workspace agent for job {job_id} in {workspace.path}")
            result = await cloud_executor.run_agent(
                task=job_task,
                workspace_path=workspace.path,
                model=job_model,
                resume_session_id=prepared_resume_id,
            )

            # 5. Capture git diff (best-effort, don't fail job on diff errors)
            try:
                diff = await workspace_manager.capture_diff(workspace)
                if diff:
                    if artifact_store:
                        artifact_store.save_artifact(worker_id, "diff.patch", diff)
                    logger.info(f"Captured diff for job {job_id}: {len(diff)} bytes")
            except Exception as diff_error:
                logger.warning(f"Failed to capture diff for job {job_id}: {diff_error}")
                diff = ""  # Ensure diff is empty on error

            # 6. Ship session to Life Hub (best-effort, for future resumption)
            if result and result.status == "success":
                try:
                    from zerg.services.session_continuity import ship_session_to_life_hub

                    await ship_session_to_life_hub(
                        workspace_path=workspace.path,
                        worker_id=worker_id,
                    )
                except Exception as ship_error:
                    logger.warning(f"Failed to ship session for job {job_id}: {ship_error}")

        except Exception as e:
            logger.exception(f"Cloud execution failed for job {job_id}")
            execution_error = str(e)

            if worker_id and artifact_store:
                try:
                    artifact_store.complete_worker(worker_id, status="failed", error=str(e))
                except Exception:
                    pass

        # 7. Open NEW short-lived session to update final job status
        with db_session() as update_db:
            update_job = update_db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id).first()
            if not update_job:
                logger.error(f"Job {job_id} not found when updating final status")
                return

            update_job.finished_at = datetime.now(timezone.utc)

            if execution_error:
                update_job.status = "failed"
                update_job.error = execution_error
                logger.error(f"Workspace worker job {job_id} failed: {execution_error}")
            elif result and result.status == "success":
                update_job.status = "success"
                logger.info(f"Workspace worker job {job_id} completed successfully")
            else:
                update_job.status = "failed"
                update_job.error = result.error if result else "Unknown error"
                logger.error(f"Workspace worker job {job_id} failed: {update_job.error}")

            final_status = update_job.status
            final_error = update_job.error if update_job.status == "failed" else None
            update_db.commit()

        # 8. Save artifacts (best-effort - failures should NOT change job status)
        if result and artifact_store:
            try:
                artifact_store.save_result(worker_id, result.output or "(No output)")
            except Exception as save_error:
                logger.warning(f"Failed to save result artifact for job {job_id}: {save_error}")

            try:
                artifact_store.complete_worker(
                    worker_id,
                    status="success" if result.status == "success" else "failed",
                    error=result.error if result.status != "success" else None,
                )
            except Exception as complete_error:
                logger.warning(f"Failed to complete artifact worker for job {job_id}: {complete_error}")

        # 9. Emit completion event for SSE (if supervisor run exists)
        # IMPORTANT: These are best-effort operations. Failures here should NOT
        # change the job status - the job already succeeded/failed above.
        if supervisor_run_id:
            # Use a new short-lived session for event emission
            with db_session() as event_db:
                try:
                    from zerg.services.event_store import emit_run_event

                    await emit_run_event(
                        db=event_db,
                        run_id=supervisor_run_id,
                        event_type="commis_complete",
                        payload={
                            "job_id": job_id,
                            "commis_id": worker_id,
                            "status": final_status,
                            "error": final_error,
                            "duration_ms": result.duration_ms if result else 0,
                            "owner_id": job_owner_id,
                            "execution_mode": "workspace",
                            "has_diff": bool(diff),
                            "trace_id": job_trace_id,
                        },
                    )
                except Exception as emit_error:
                    logger.warning(f"Failed to emit SSE event for job {job_id}: {emit_error}")

            # Resume supervisor if waiting (best-effort) - use another short session
            with db_session() as resume_db:
                try:
                    from zerg.services.worker_resume import resume_supervisor_with_worker_result

                    summary = result.output[:500] if result and result.output else "(No output)"
                    if diff:
                        summary += f"\n\n[Git diff captured: {len(diff)} bytes]"

                    await resume_supervisor_with_worker_result(
                        db=resume_db,
                        run_id=supervisor_run_id,
                        worker_result=summary,
                        job_id=job_id,
                    )
                except Exception as resume_error:
                    logger.warning(f"Failed to resume supervisor for job {job_id}: {resume_error}")

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
