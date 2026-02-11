"""Commis Job Processor Service.

This service manages the execution of commis jobs in the background.
It polls for queued commis jobs and executes them in workspace mode (hatch subprocess).

Events emitted (for SSE streaming):
- COMMIS_SPAWNED: When a job is picked up for processing
- COMMIS_STARTED: When commis execution begins
- COMMIS_COMPLETE: When commis finishes (success/failed/timeout)
- COMMIS_SUMMARY_READY: When summary extraction completes

SQLite-safe concurrency:
- Uses dialect-aware job claiming (Postgres: FOR UPDATE SKIP LOCKED, SQLite: BEGIN IMMEDIATE)
- Heartbeat tracking for stale job detection
- Automatic reclaim of jobs from dead workers
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Optional

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import db_session
from zerg.database import list_test_commis_ids
from zerg.database import reset_test_commis_id
from zerg.database import set_test_commis_id
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.commis_job_queue import HEARTBEAT_INTERVAL_SECONDS
from zerg.services.commis_job_queue import claim_jobs
from zerg.services.commis_job_queue import get_worker_id
from zerg.services.commis_job_queue import reclaim_stale_jobs
from zerg.services.commis_job_queue import update_heartbeat

logger = logging.getLogger(__name__)

# Summary max length
_SUMMARY_MAX_LENGTH = 150
_DEFAULT_CALLBACK_PORT = 8080


def _extract_summary_from_output(
    output: Optional[str],
    *,
    status: str = "success",
    error: Optional[str] = None,
) -> str:
    """Extract a concise summary from hatch output, with status context.

    For failures/timeouts/cancellations, prioritizes error information over empty stdout.

    Args:
        output: Raw hatch output text (stdout)
        status: Execution status ("success", "failed", "timeout")
        error: Error message (stderr or exception message) for failures

    Returns:
        Summary text (max 150 chars) with status prefix for failures
    """
    # For failures/timeouts/cancellations, prioritize error information
    if status in ("failed", "timeout", "cancelled"):
        if status == "failed":
            prefix = "[FAILED] "
        elif status == "timeout":
            prefix = "[TIMEOUT] "
        else:
            prefix = "[CANCELLED] "
        prefix_len = len(prefix)
        remaining_len = _SUMMARY_MAX_LENGTH - prefix_len

        # Priority 1: Use error message if available
        if error and error.strip():
            error_text = " ".join(error.strip().replace("\n", " ").replace("\r", " ").split())
            if len(error_text) <= remaining_len:
                return prefix + error_text
            truncated = error_text[: remaining_len - 3]
            last_space = truncated.rfind(" ")
            if last_space > remaining_len // 2:
                truncated = truncated[:last_space]
            return prefix + truncated + "..."

        # Priority 2: Use output if error is empty
        if output and output.strip():
            output_text = " ".join(output.strip().replace("\n", " ").replace("\r", " ").split())
            if len(output_text) <= remaining_len:
                return prefix + output_text
            truncated = output_text[: remaining_len - 3]
            last_space = truncated.rfind(" ")
            if last_space > remaining_len // 2:
                truncated = truncated[:last_space]
            return prefix + truncated + "..."

        # Fallback for failures with no output or error
        return prefix + "(No error details available)"

    # Success case: just use output
    if not output or not output.strip():
        return "(No output)"

    # Take first 150 chars, clean up newlines, truncate at word boundary
    text = " ".join(output.strip().replace("\n", " ").replace("\r", " ").split())
    if len(text) <= _SUMMARY_MAX_LENGTH:
        return text

    # Find last space before limit to avoid cutting mid-word
    truncated = text[:_SUMMARY_MAX_LENGTH]
    last_space = truncated.rfind(" ")
    if last_space > _SUMMARY_MAX_LENGTH // 2:
        truncated = truncated[:last_space]

    return truncated + "..."


def _default_callback_url() -> str:
    return f"http://localhost:{_DEFAULT_CALLBACK_PORT}"


def _build_hook_env(job_id: int) -> dict[str, str]:
    settings = get_settings()
    callback_url = os.environ.get("LONGHOUSE_CALLBACK_URL") or _default_callback_url()
    env_vars = {
        "LONGHOUSE_CALLBACK_URL": callback_url,
        "COMMIS_JOB_ID": str(job_id),
    }
    if settings.internal_api_secret:
        env_vars["COMMIS_CALLBACK_TOKEN"] = settings.internal_api_secret
    return env_vars


def _ingest_workspace_session(
    workspace_path: Path,
    job_id: int,
    commis_id: str,
    job_started_at: datetime,
) -> None:
    """Ingest Claude Code session JSONL from a workspace into the agent timeline.

    Finds the session JSONL that Claude Code wrote during workspace execution,
    parses it using the shipper parser, and ingests directly via AgentsStore.

    This is best-effort: failures are logged but don't affect job status.
    Deduplication is handled by event_hash in AgentsStore.ingest_session().
    """
    from zerg.services.agents_store import AgentsStore
    from zerg.services.agents_store import EventIngest
    from zerg.services.agents_store import SessionIngest
    from zerg.services.session_continuity import encode_cwd_for_claude
    from zerg.services.session_continuity import get_claude_config_dir
    from zerg.services.shipper.parser import extract_session_metadata
    from zerg.services.shipper.parser import parse_session_file

    config_dir = get_claude_config_dir()
    encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
    session_dir = config_dir / "projects" / encoded_cwd

    if not session_dir.exists():
        logger.debug(f"No Claude session dir for workspace job {job_id}: {session_dir}")
        return

    # Find JSONL files modified after the job started
    # Normalize job_started_at to naive UTC for comparison (SQLite returns naive datetimes)
    started_naive = job_started_at.replace(tzinfo=None) if job_started_at.tzinfo else job_started_at
    candidates = [p for p in session_dir.glob("*.jsonl") if datetime.utcfromtimestamp(p.stat().st_mtime) >= started_naive]

    if not candidates:
        logger.debug(f"No new JSONL files for workspace job {job_id}")
        return

    # Pick the most recently modified file
    session_file = max(candidates, key=lambda p: p.stat().st_mtime)
    logger.info(f"Ingesting workspace session from {session_file} for job {job_id}")

    # Parse using shipper parser
    events = list(parse_session_file(session_file))
    if not events:
        logger.debug(f"No events parsed from {session_file} for job {job_id}")
        return

    metadata = extract_session_metadata(session_file)
    source_path = str(session_file)

    # Convert ParsedEvents to EventIngest
    event_ingests = [
        EventIngest(
            role=e.role,
            content_text=e.content_text,
            tool_name=e.tool_name,
            tool_input_json=e.tool_input_json,
            tool_output_text=e.tool_output_text,
            timestamp=e.timestamp,
            source_path=source_path,
            source_offset=e.source_offset,
            raw_json=e.raw_line if e.raw_line else None,
        )
        for e in events
    ]

    # Determine timestamps
    timestamps = [e.timestamp for e in events if e.timestamp]
    started_at = metadata.started_at or (min(timestamps) if timestamps else datetime.now(timezone.utc))
    ended_at = metadata.ended_at or (max(timestamps) if timestamps else None)

    # Use deterministic session ID based on source file to prevent duplicates on re-ingest.
    # If the same JSONL file is ingested twice, the same session UUID is generated.
    import uuid as uuid_mod

    provider_session_id = metadata.session_id or session_file.stem
    session_id = uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, f"commis:{source_path}")

    session_ingest = SessionIngest(
        id=session_id,
        provider="claude",
        environment="commis",
        project=metadata.project,
        device_id=f"commis-{commis_id}",
        cwd=metadata.cwd or str(workspace_path),
        git_branch=metadata.git_branch,
        started_at=started_at,
        ended_at=ended_at,
        provider_session_id=provider_session_id,
        events=event_ingests,
    )

    with db_session() as db:
        store = AgentsStore(db)
        result = store.ingest_session(session_ingest)
        logger.info(
            f"Ingested workspace session for job {job_id}: "
            f"{result.events_inserted} events inserted, "
            f"{result.events_skipped} skipped"
        )


class CommisJobProcessor:
    """Service to process queued commis jobs in the background.

    SQLite-safe: Uses dialect-aware job claiming and heartbeat tracking
    to enable concurrent job processing without Postgres-specific locks.
    """

    def __init__(self):
        """Initialize the commis job processor."""
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stale_reclaim_task: Optional[asyncio.Task] = None
        # Interactive latency matters: commis are typically spawned from chat flows.
        # Keep polling reasonably tight so a queued job starts quickly.
        self._check_interval = 1  # seconds
        self._max_concurrent_jobs = 5  # Process up to 5 jobs concurrently
        self._worker_id = get_worker_id()
        # Track running jobs for heartbeat updates (commis_id, job_id)
        self._running_jobs: set[tuple[str | None, int]] = set()

    async def start(self) -> None:
        """Start the commis job processor."""
        if self._running:
            logger.warning("Commis job processor already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._process_jobs_loop())
        self._stale_reclaim_task = asyncio.create_task(self._stale_reclaim_loop())
        logger.info(f"Commis job processor started (worker_id={self._worker_id})")

    async def stop(self) -> None:
        """Stop the commis job processor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._stale_reclaim_task:
            self._stale_reclaim_task.cancel()
            try:
                await self._stale_reclaim_task
            except asyncio.CancelledError:
                pass
        logger.info("Commis job processor stopped")

    async def _process_jobs_loop(self) -> None:
        """Main processing loop for commis jobs."""
        while self._running:
            try:
                await self._process_pending_jobs()
            except Exception as e:
                logger.exception(f"Error in commis job processing loop: {e}")

            await asyncio.sleep(self._check_interval)

    async def _stale_reclaim_loop(self) -> None:
        """Periodically reclaim jobs from dead workers.

        Runs every HEARTBEAT_INTERVAL_SECONDS and reclaims any jobs
        that haven't received a heartbeat within STALE_THRESHOLD_SECONDS.
        """
        while self._running:
            try:
                for commis_id in self._iter_commis_ids():
                    token = set_test_commis_id(commis_id) if commis_id else None
                    try:
                        with db_session() as db:
                            reclaimed = reclaim_stale_jobs(db)
                            if reclaimed > 0:
                                label = commis_id or "default"
                                logger.info(f"Reclaimed {reclaimed} stale commis jobs (db={label})")
                    finally:
                        if token is not None:
                            reset_test_commis_id(token)
            except Exception as e:
                logger.exception(f"Error in stale job reclaim loop: {e}")

            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    async def _heartbeat_loop(self, commis_id: str | None, job_id: int) -> None:
        """Send periodic heartbeats for a running job.

        This runs in parallel with job execution to prove this worker
        is still alive. If the worker crashes, heartbeats stop, and
        the stale reclaim loop will reset the job to 'queued'.

        Args:
            job_id: The job ID to send heartbeats for
        """
        token = set_test_commis_id(commis_id) if commis_id else None
        try:
            while (commis_id, job_id) in self._running_jobs:
                try:
                    with db_session() as db:
                        success = update_heartbeat(db, job_id, self._worker_id)
                        if not success:
                            # Job was cancelled or reclaimed - stop heartbeating
                            logger.warning(f"Heartbeat failed for job {job_id} - job may have been reclaimed")
                            break
                except Exception as e:
                    logger.warning(f"Error updating heartbeat for job {job_id}: {e}")

                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        finally:
            if token is not None:
                reset_test_commis_id(token)

    async def _process_pending_jobs(self) -> None:
        """Process pending jobs from default schema (normal mode).

        Uses dialect-aware atomic job claiming:
        - Postgres: FOR UPDATE SKIP LOCKED
        - SQLite: BEGIN IMMEDIATE + UPDATE RETURNING
        """
        tasks = []
        total_claimed = 0

        for commis_id in self._iter_commis_ids():
            token = set_test_commis_id(commis_id) if commis_id else None
            try:
                with db_session() as db:
                    job_ids = claim_jobs(db, self._max_concurrent_jobs, self._worker_id)
            finally:
                if token is not None:
                    reset_test_commis_id(token)

            if not job_ids:
                continue

            total_claimed += len(job_ids)
            label = commis_id or "default"
            logger.info(f"Claimed {len(job_ids)} queued commis jobs atomically (db={label})")

            for job_id in job_ids:
                self._running_jobs.add((commis_id, job_id))
                asyncio.create_task(self._heartbeat_loop(commis_id, job_id))
                tasks.append(asyncio.create_task(self._process_job_with_cleanup(commis_id, job_id)))

        if not tasks:
            return

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_job_with_cleanup(self, commis_id: str | None, job_id: int) -> None:
        """Process a job and clean up tracking state when done."""
        token = set_test_commis_id(commis_id) if commis_id else None
        try:
            await self._process_job_by_id(job_id, already_claimed=True)
        finally:
            # Remove from running jobs set (stops heartbeat loop)
            self._running_jobs.discard((commis_id, job_id))
            if token is not None:
                reset_test_commis_id(token)

    def _iter_commis_ids(self) -> list[str | None]:
        """Return commis IDs to process in E2E; include default DB."""
        settings = get_settings()
        if settings.testing and settings.environment == "test:e2e":
            ids = list_test_commis_ids()
            return [None, *ids] if ids else [None]
        return [None]

    async def _process_job_by_id(self, job_id: int, *, already_claimed: bool = False) -> None:
        """Process a single commis job by ID with its own database session.

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
        execution_mode = "workspace"
        oikos_run_id = None
        job_task_preview = ""

        with db_session() as db:
            job = db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id).first()
            if not job:
                logger.warning(f"Job {job_id} not found - may have been deleted")
                return

            if not already_claimed:
                # Check if job is still queued (another processor may have grabbed it)
                if job.status != "queued":
                    logger.debug(f"Job {job_id} already being processed (status: {job.status})")
                    return

            # Determine execution mode (workspace is default since 2026-02)
            job_config = job.config or {}
            execution_mode = job_config.get("execution_mode", "workspace")

            # Standard mode is deprecated — only allow with LEGACY_STANDARD_MODE=1
            if execution_mode == "standard":
                import os

                legacy_flag = os.environ.get("LEGACY_STANDARD_MODE", "")
                if legacy_flag not in ("1", "true", "yes"):
                    # Only force workspace if we have a git_repo; otherwise standard mode
                    # is the only viable path (no workspace without a repo to clone)
                    git_repo_for_check = job_config.get("git_repo")
                    if git_repo_for_check:
                        logger.warning(
                            f"Commis job {job_id} requested standard mode but LEGACY_STANDARD_MODE is not set. "
                            "Standard mode is deprecated — forcing workspace mode."
                        )
                        execution_mode = "workspace"
                    else:
                        logger.warning(
                            f"Commis job {job_id} using deprecated standard mode (no git_repo, "
                            "cannot force workspace). Set LEGACY_STANDARD_MODE=1 to suppress."
                        )
                else:
                    logger.warning(f"Commis job {job_id} using deprecated standard mode (LEGACY_STANDARD_MODE=1).")

            if execution_mode not in {"standard", "workspace"}:
                job.status = "failed"
                job.error = f"Invalid execution_mode: {execution_mode}"
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
                logger.error(f"Commis job {job_id} failed: invalid execution_mode '{execution_mode}'")
                return

            # Capture oikos run ID for SSE correlation
            oikos_run_id = job.oikos_run_id
            job_task_preview = job.task[:50] if job.task else ""

            if not already_claimed:
                # Update job status to running (only if not already claimed atomically)
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                db.commit()

        # Session is now closed - execute outside of any db session context
        logger.info(f"Starting commis job {job_id} (mode={execution_mode}) for task: {job_task_preview}...")

        if execution_mode == "workspace":
            # Workspace execution: manages its own short-lived sessions
            # No db session passed - _process_workspace_job opens/closes sessions as needed
            await self._process_workspace_job(job_id, oikos_run_id)
        else:
            # Standard mode is removed — fail the job with a clear error
            with db_session() as err_db:
                err_job = err_db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id).first()
                if err_job:
                    err_job.status = "failed"
                    err_job.error = (
                        "Standard mode (in-process CommisRunner) has been removed. " "All commis must use workspace mode with a git_repo."
                    )
                    err_job.finished_at = datetime.now(timezone.utc)
                    err_db.commit()
            logger.error(f"Commis job {job_id} failed: standard mode is removed")

    async def _process_workspace_job(self, job_id: int, oikos_run_id: Optional[int]) -> None:
        """Process a job using workspace execution (hatch subprocess with git workspace).

        This enables 24/7 execution on zerg-vps independent of laptop connectivity.
        The commis runs in a cloned git workspace and changes are captured as a diff.

        This method manages its own short-lived db sessions to avoid exhausting
        the connection pool during long-running workspace execution.
        """
        import uuid

        from zerg.services.cloud_executor import CloudExecutor
        from zerg.services.workspace_manager import WorkspaceManager

        # Extract all needed data from job in a short-lived session
        with db_session() as db:
            job = db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id).first()
            if not job:
                logger.error(f"Job {job_id} not found when starting workspace execution")
                return

            job_task = job.task
            job_model = job.model
            job_owner_id = job.owner_id
            job_trace_id = str(job.trace_id) if job.trace_id else None
            job_started_at = job.started_at or datetime.now(timezone.utc)
            job_config = job.config or {}
            git_repo = job_config.get("git_repo")
            resume_session_id = job_config.get("resume_session_id")

            if not git_repo:
                job.status = "failed"
                job.error = "Workspace execution requires git_repo in job config"
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
                logger.error(f"Commis job {job_id} failed: missing git_repo")
                return

            # Generate a unique commis_id for artifact storage
            commis_id = f"ws-{job_id}-{uuid.uuid4().hex[:8]}"
            job.commis_id = commis_id
            base_branch = job_config.get("base_branch", "main")

            # Commit commis_id before long-running execution
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
            artifact_store = CommisArtifactStore()
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
                run_id=commis_id,
                base_branch=base_branch,
            )

            # 2. Create commis directory for artifacts (best-effort, don't fail job)
            if artifact_store:
                try:
                    artifact_store.create_commis(
                        task=job_task,
                        config={
                            "execution_mode": "workspace",
                            "git_repo": git_repo,
                            "workspace_path": str(workspace.path),
                        },
                        commis_id=commis_id,
                    )
                    artifact_store.start_commis(commis_id)
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

            # 4. Emit commis_started event for UI (before long-running execution)
            if oikos_run_id:
                try:
                    from zerg.services.event_store import append_run_event

                    await append_run_event(
                        run_id=oikos_run_id,
                        event_type="commis_started",
                        payload={
                            "job_id": job_id,
                            "commis_id": commis_id,
                            "task": job_task[:100] if job_task else "",
                        },
                    )
                except Exception as started_error:
                    logger.warning(f"Failed to emit commis_started event for job {job_id}: {started_error}")

            # 5. Run commis in workspace (LONG-RUNNING - no DB session held!)
            logger.info(f"Running workspace commis for job {job_id} in {workspace.path}")
            hook_env = _build_hook_env(job_id)
            result = await cloud_executor.run_commis(
                task=job_task,
                workspace_path=workspace.path,
                model=job_model,
                resume_session_id=prepared_resume_id,
                env_vars=hook_env,
            )

            # 6. Capture git diff (best-effort, don't fail job on diff errors)
            try:
                diff = await workspace_manager.capture_diff(workspace)
                if diff:
                    if artifact_store:
                        artifact_store.save_artifact(commis_id, "diff.patch", diff)
                    logger.info(f"Captured diff for job {job_id}: {len(diff)} bytes")
            except Exception as diff_error:
                logger.warning(f"Failed to capture diff for job {job_id}: {diff_error}")
                diff = ""  # Ensure diff is empty on error

            # 7. Ingest session JSONL into agent timeline (best-effort)
            if result and result.status == "success":
                try:
                    _ingest_workspace_session(
                        workspace_path=workspace.path,
                        job_id=job_id,
                        commis_id=commis_id,
                        job_started_at=job_started_at,
                    )
                except Exception as ingest_error:
                    logger.warning(f"Failed to ingest workspace session for job {job_id}: {ingest_error}")

        except Exception as e:
            logger.exception(f"Cloud execution failed for job {job_id}")
            execution_error = str(e)

            if commis_id and artifact_store:
                try:
                    artifact_store.complete_commis(commis_id, status="failed", error=str(e))
                except Exception:
                    pass

        # 7. Open NEW short-lived session to update final job status
        with db_session() as update_db:
            update_job = update_db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id).first()
            if not update_job:
                logger.error(f"Job {job_id} not found when updating final status")
                return

            # Check if job was cancelled while running - don't overwrite cancelled status
            if update_job.status == "cancelled":
                logger.info(f"Workspace commis job {job_id} was cancelled - preserving cancelled status")
                final_status = "cancelled"
                final_error = update_job.error
                # Don't update anything, status is already correct
            else:
                update_job.finished_at = datetime.now(timezone.utc)

                if execution_error:
                    update_job.status = "failed"
                    update_job.error = execution_error
                    logger.error(f"Workspace commis job {job_id} failed: {execution_error}")
                elif result and result.status == "success":
                    update_job.status = "success"
                    logger.info(f"Workspace commis job {job_id} completed successfully")
                else:
                    update_job.status = "failed"
                    update_job.error = result.error if result else "Unknown error"
                    logger.error(f"Workspace commis job {job_id} failed: {update_job.error}")

                final_status = update_job.status
                final_error = update_job.error if update_job.status == "failed" else None
                update_db.commit()

        # 8. Save artifacts (best-effort - failures should NOT change job status)
        # For cancelled jobs, still save partial results if available
        if result and artifact_store:
            try:
                artifact_store.save_result(commis_id, result.output or "(No output)")
            except Exception as save_error:
                logger.warning(f"Failed to save result artifact for job {job_id}: {save_error}")

            try:
                artifact_store.complete_commis(
                    commis_id,
                    status=final_status,  # Use final_status which respects cancelled state
                    error=final_error,
                )
            except Exception as complete_error:
                logger.warning(f"Failed to complete artifact commis for job {job_id}: {complete_error}")

        # 9. Emit completion event for SSE (if oikos run exists)
        # IMPORTANT: These are best-effort operations. Failures here should NOT
        # change the job status - the job already succeeded/failed above.
        if oikos_run_id:
            # Use a new short-lived session for event emission
            with db_session() as event_db:
                try:
                    from zerg.services.event_store import emit_run_event

                    await emit_run_event(
                        db=event_db,
                        run_id=oikos_run_id,
                        event_type="commis_complete",
                        payload={
                            "job_id": job_id,
                            "commis_id": commis_id,
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

            # Generate and emit summary for UI (best-effort)
            # Use _extract_summary_from_output to create informative summaries for failures
            summary = _extract_summary_from_output(
                result.output if result else None,
                status=final_status,
                error=final_error or (result.error if result else None),
            )
            with db_session() as summary_db:
                try:
                    # Save summary to artifact store
                    if artifact_store:
                        artifact_store.update_summary(commis_id, summary, {"source": "hatch_output"})

                    # Emit summary ready event for UI with status context
                    from zerg.services.event_store import emit_run_event

                    await emit_run_event(
                        db=summary_db,
                        run_id=oikos_run_id,
                        event_type="commis_summary_ready",
                        payload={
                            "job_id": job_id,
                            "commis_id": commis_id,
                            "summary": summary,
                            "status": final_status,
                            "error": final_error,
                        },
                    )
                except Exception as summary_error:
                    logger.warning(f"Failed to emit summary for job {job_id}: {summary_error}")

            # Resume oikos if waiting (best-effort) - use another short session
            # Skip resume for cancelled jobs - user explicitly cancelled, don't continue the run
            if final_status != "cancelled":
                with db_session() as resume_db:
                    try:
                        from zerg.services.commis_resume import resume_oikos_with_commis_result

                        resume_summary = summary
                        if diff:
                            resume_summary += f"\n\n[Git diff captured: {len(diff)} bytes]"

                        await resume_oikos_with_commis_result(
                            db=resume_db,
                            run_id=oikos_run_id,
                            commis_result=resume_summary,
                            job_id=job_id,
                        )
                    except Exception as resume_error:
                        logger.warning(f"Failed to resume oikos for job {job_id}: {resume_error}")
            else:
                logger.info(f"Skipping oikos resume for cancelled job {job_id}")

    async def process_job_now(self, job_id: int) -> bool:
        """Process a specific job immediately (for testing/debugging).

        Args:
            job_id: The job ID to process

        Returns:
            True if job was found and processed, False otherwise
        """
        with db_session() as db:
            job = db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id).first()
            if not job:
                return False

            if job.status != "queued":
                logger.warning(f"Job {job_id} is not in queued state (status: {job.status})")
                return False

        # Process with its own session
        await self._process_job_by_id(job_id)
        return True


# Singleton instance for application-wide use
commis_job_processor = CommisJobProcessor()
