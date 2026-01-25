"""Concierge tools for spawning and managing commis agents.

This module provides tools that allow concierge agents to delegate tasks to
disposable commis agents, retrieve their results, and drill into their artifacts.

The concierge/commis pattern enables complex delegation scenarios where a concierge
can spawn multiple commis for parallel execution or break down complex tasks.

Commis execution flow:
- spawn_commis() creates WorkerJob and returns job info
- Caller (supervisor_react_engine) raises AgentInterrupted to pause
- Commis runs in background via WorkerJobProcessor
- Commis completion triggers resume via worker_resume.py
- Concierge resumes with commis result injected as tool response
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.models_config import DEFAULT_WORKER_MODEL_ID
from zerg.services.supervisor_context import get_supervisor_context
from zerg.services.tool_output_store import ToolOutputStore
from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)


async def spawn_worker_async(
    task: str,
    model: str | None = None,
    execution_mode: str = "standard",
    git_repo: str | None = None,
    resume_session_id: str | None = None,
    *,
    _tool_call_id: str | None = None,  # Internal: passed by _call_tool_async for idempotency
    _skip_interrupt: bool = False,  # Internal: legacy param, now ignored
    _return_structured: bool = False,  # Internal: return dict instead of string for supervisor_react_engine
) -> str | dict:
    """Spawn a worker agent to execute a task and wait for completion.

    The worker runs in the background. Creates a WorkerJob and returns job info
    for the caller to handle interruption and resumption.

    Args:
        task: Natural language description of what the worker should do
        model: LLM model for the worker (default: gpt-5-mini)
        execution_mode: "standard" (default) runs via WebSocket runner, "workspace"
            runs headless on the server in a git workspace. Accepts "local" and
            "cloud" for backward compatibility.
        git_repo: Git repository URL (required if execution_mode="workspace").
            The repo is cloned, agent makes changes, and diff is captured.
        resume_session_id: Life Hub session UUID to resume (workspace mode only).
            Enables cross-environment session continuity.

    Returns:
        The worker's result after completion

    Example:
        spawn_worker("Check disk usage on prod-web server via SSH")
        spawn_worker("Research vacuums and recommend the best one")
        spawn_worker("Fix typo in README", execution_mode="workspace", git_repo="git@github.com:user/repo.git")
        spawn_worker("Continue work", execution_mode="workspace", git_repo="...", resume_session_id="abc-123")
    """
    from zerg.models.models import WorkerJob

    # Validate execution_mode and git_repo combination
    # Accept both old names (local, cloud) and new names (standard, workspace) for backward compat
    valid_modes = {"local", "cloud", "standard", "workspace"}
    if execution_mode not in valid_modes:
        return f"Error: execution_mode must be 'standard' or 'workspace', got '{execution_mode}'"

    # Workspace mode (cloud is alias) requires git_repo
    if execution_mode in ("cloud", "workspace") and not git_repo:
        return "Error: git_repo is required when execution_mode='workspace'"

    # Get database session from credential resolver context
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot spawn worker - no credential context available"

    db = resolver.db
    owner_id = resolver.owner_id

    # Get supervisor run_id and trace_id from context (for SSE event correlation and debugging)
    ctx = get_supervisor_context()
    supervisor_run_id = ctx.run_id if ctx else None
    trace_id = ctx.trace_id if ctx else None

    # Worker inherits model and reasoning_effort from supervisor context
    # Priority: explicit arg > supervisor context > default
    worker_model = model or (ctx.model if ctx else None) or DEFAULT_WORKER_MODEL_ID
    worker_reasoning_effort = (ctx.reasoning_effort if ctx else None) or "none"

    # Build execution config for workspace mode (cloud is alias for backward compat)
    job_config = None
    if execution_mode in ("cloud", "workspace"):
        job_config = {
            "execution_mode": "workspace",  # Normalize to new name
            "git_repo": git_repo,
        }
        if resume_session_id:
            job_config["resume_session_id"] = resume_session_id

    try:
        # IDEMPOTENCY: Prevent duplicate workers on retry/resume.
        #
        # Primary strategy: Use tool_call_id (unique per LLM response) for exact idempotency.
        # Fallback strategy: Prefix matching on task string (handles cases without tool_call_id).
        #
        # This prevents duplicate workers while allowing legitimate multi-worker scenarios.

        worker_job = None
        existing_job = None

        # PRIMARY: Check for existing job with same tool_call_id (most reliable)
        if _tool_call_id and supervisor_run_id:
            existing_job = (
                db.query(WorkerJob)
                .filter(
                    WorkerJob.supervisor_run_id == supervisor_run_id,
                    WorkerJob.tool_call_id == _tool_call_id,
                )
                .first()
            )
            if existing_job:
                logger.info(f"[IDEMPOTENT] Found existing job {existing_job.id} for tool_call_id={_tool_call_id}")

        # FALLBACK: Check for completed/in-progress workers using task matching
        # (only if tool_call_id lookup didn't find anything)
        if existing_job is None:
            completed_jobs = (
                db.query(WorkerJob)
                .filter(
                    WorkerJob.supervisor_run_id == supervisor_run_id,
                    WorkerJob.owner_id == owner_id,
                    WorkerJob.status == "success",
                )
                .order_by(WorkerJob.created_at.desc())
                .limit(20)
                .all()
            )

            if completed_jobs:
                # Exact task match only - prefix matching was removed as unsafe
                # (near-matches could return wrong worker results if tasks share prefixes)
                for job in completed_jobs:
                    if job.task == task:
                        existing_job = job
                        break

        if existing_job is None:
            # No completed match - check for in-progress job with EXACT task match
            existing_job = (
                db.query(WorkerJob)
                .filter(
                    WorkerJob.supervisor_run_id == supervisor_run_id,
                    WorkerJob.task == task,
                    WorkerJob.owner_id == owner_id,
                    WorkerJob.status.in_(["queued", "running"]),
                )
                .first()
            )

        if existing_job:
            if existing_job.status == "success":
                # Already completed - return cached result immediately
                # This prevents duplicate workers on retry
                logger.debug(f"Existing job {existing_job.id} already succeeded, returning cached result")
                if existing_job.worker_id:
                    try:
                        artifact_store = WorkerArtifactStore()
                        # Use summary-first approach (consistent with resume path)
                        metadata = artifact_store.get_worker_metadata(existing_job.worker_id)
                        summary = metadata.get("summary")
                        if summary:
                            return f"Worker job {existing_job.id} completed:\n\n{summary}"
                        # Fall back to full result if no summary
                        result = artifact_store.get_worker_result(existing_job.worker_id)
                        return f"Worker job {existing_job.id} completed:\n\n{result}"
                    except FileNotFoundError:
                        # Result artifact not available, treat as if job doesn't exist
                        logger.warning(f"Job {existing_job.id} SUCCESS but no result artifact, creating new job")
                else:
                    logger.warning(f"Job {existing_job.id} SUCCESS but no worker_id, creating new job")
                # Fall through to create new job
            else:
                # queued or running - reuse and wait via interrupt
                worker_job = existing_job
                logger.debug(f"Reusing existing worker job {worker_job.id} (status: {existing_job.status})")

        if worker_job is None:
            # Create new worker job record with tool_call_id for idempotency
            import uuid as uuid_module

            worker_job = WorkerJob(
                owner_id=owner_id,
                supervisor_run_id=supervisor_run_id,
                tool_call_id=_tool_call_id,  # Enables idempotency on retry/resume
                trace_id=uuid_module.UUID(trace_id) if trace_id else None,  # Inherit from supervisor for debugging
                task=task,
                model=worker_model,
                reasoning_effort=worker_reasoning_effort,  # Inherit from supervisor
                status="queued",
                config=job_config,  # Cloud execution config (execution_mode, git_repo)
            )
            db.add(worker_job)
            db.commit()
            db.refresh(worker_job)
            logger.info(f"[SPAWN] Created worker job {worker_job.id} with tool_call_id={_tool_call_id}")

            # Emit WORKER_SPAWNED event durably (replays on reconnect)
            # Only persist if we have a supervisor run_id (test mocks may not have one)
            if supervisor_run_id is not None:
                from zerg.services.event_store import append_run_event

                await append_run_event(
                    run_id=supervisor_run_id,
                    event_type="commis_spawned",
                    payload={
                        "job_id": worker_job.id,
                        "tool_call_id": _tool_call_id,
                        "task": task[:100],
                        "model": worker_model,
                        "owner_id": owner_id,
                        "trace_id": trace_id,
                    },
                )

        # Return job info for caller to handle interruption.
        # supervisor_react_engine raises AgentInterrupted
        # to pause execution until worker completes.
        logger.debug(f"spawn_worker returning queued response for job {worker_job.id}")
        if _return_structured:
            return {"job_id": worker_job.id, "status": "queued", "task": task[:100]}
        return f"Worker job {worker_job.id} queued successfully. Working on: {task[:100]}"

    except Exception as e:
        logger.exception(f"Failed to spawn worker for task: {task}")
        db.rollback()
        return f"Error spawning worker: {e}"


async def spawn_standard_worker_async(
    task: str,
    model: str | None = None,
    *,
    _tool_call_id: str | None = None,
    _return_structured: bool = False,
) -> str | dict:
    """Spawn a worker for general tasks (server commands, research, etc).

    For repository/code tasks, use spawn_workspace_worker instead.

    Args:
        task: Natural language description of what the worker should do
        model: LLM model for the worker (optional)

    Returns:
        The worker's result after completion
    """
    return await spawn_worker_async(
        task=task,
        model=model,
        execution_mode="standard",
        git_repo=None,
        _tool_call_id=_tool_call_id,
        _return_structured=_return_structured,
    )


def spawn_worker(
    task: str,
    model: str | None = None,
) -> str:
    """Spawn a worker for general tasks (server commands, research, etc).

    For repository/code tasks, use spawn_workspace_worker instead.
    """
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(spawn_standard_worker_async(task, model))


async def spawn_workspace_worker_async(
    task: str,
    git_repo: str,
    model: str | None = None,
    resume_session_id: str | None = None,
    *,
    _tool_call_id: str | None = None,
    _return_structured: bool = False,
) -> str | dict:
    """Spawn a worker to execute a task in a git repository workspace.

    The repository is cloned to an isolated workspace, the agent runs
    headlessly, and any changes are captured as a diff.

    Args:
        task: What to do in the repository (analyze code, fix bug, etc)
        git_repo: Repository URL (https://github.com/org/repo.git or git@github.com:org/repo.git)
        model: LLM model for the worker (optional)
        resume_session_id: Life Hub session UUID to resume (for session continuity)

    Returns:
        The worker's result after completion

    Example:
        spawn_workspace_worker("List dependencies from pyproject.toml", "https://github.com/langchain-ai/langchain.git")
        spawn_workspace_worker("Fix the typo in README.md", "git@github.com:user/repo.git")
        spawn_workspace_worker("Continue the work", "git@...", resume_session_id="abc-123")
    """
    # Early validation: reject dangerous URLs before job creation (defense in depth)
    # Delegate to shared validator to stay consistent with workspace_manager rules.
    from zerg.services.workspace_manager import validate_git_repo_url

    try:
        validate_git_repo_url(git_repo)
    except ValueError as exc:
        return f"Error: {exc}"

    # Delegate to the core implementation with workspace mode forced
    return await spawn_worker_async(
        task=task,
        model=model,
        execution_mode="workspace",
        git_repo=git_repo,
        resume_session_id=resume_session_id,
        _tool_call_id=_tool_call_id,
        _return_structured=_return_structured,
    )


def spawn_workspace_worker(
    task: str,
    git_repo: str,
    model: str | None = None,
    resume_session_id: str | None = None,
) -> str:
    """Sync wrapper for spawn_workspace_worker_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(spawn_workspace_worker_async(task, git_repo, model, resume_session_id))


async def list_workers_async(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """List recent worker jobs with SUMMARIES ONLY.

    Returns compressed summaries for scanning. To get full details,
    call read_commis_result(job_id).

    This prevents context overflow when scanning 50+ workers.

    Args:
        limit: Maximum number of jobs to return (default: 20)
        status: Filter by status ("queued", "running", "success", "failed", or None for all)
        since_hours: Only show jobs from the last N hours

    Returns:
        Formatted list of worker jobs with summaries (not full results)
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot list workers - no credential context available"

    db = resolver.db

    try:
        # Query worker jobs with filtering
        query = db.query(crud.WorkerJob).filter(crud.WorkerJob.owner_id == resolver.owner_id)

        if status:
            query = query.filter(crud.WorkerJob.status == status)

        if since_hours is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.WorkerJob.created_at >= since)

        jobs = query.order_by(crud.WorkerJob.created_at.desc()).limit(limit).all()

        if not jobs:
            return "No worker jobs found matching criteria."

        # Get artifact store for summary lookup
        artifact_store = WorkerArtifactStore()

        # Format output - compact with summaries
        lines = [f"Recent workers (showing {len(jobs)}):\n"]
        for job in jobs:
            job_id = job.id
            job_status = job.status

            # Get summary from artifact store if available, else truncate task
            summary = None
            if job.worker_id and job.status in ["success", "failed"]:
                try:
                    metadata = artifact_store.get_worker_metadata(job.worker_id)
                    summary = metadata.get("summary")
                except Exception:
                    pass  # Fall back to task truncation

            if not summary:
                # Fallback: truncate task for display
                summary = job.task[:150] + "..." if len(job.task) > 150 else job.task

            # Compact format with summary
            lines.append(f"- Job {job_id} [{job_status.upper()}]")
            lines.append(f"  {summary}\n")

        lines.append("Use read_commis_result(job_id) for full details.")
        return "\n".join(lines)

    except Exception as e:
        logger.exception("Failed to list worker jobs")
        return f"Error listing worker jobs: {e}"


def list_workers(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """Sync wrapper for list_workers_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(list_workers_async(limit, status, since_hours))


def format_duration(duration_ms: int) -> str:
    """Format duration for human readability.

    Args:
        duration_ms: Duration in milliseconds

    Returns:
        Formatted string: "123ms" for <1s, "1.2s" for 1s+, "2m 15s" for 1min+
    """
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    elif duration_ms < 60000:
        seconds = duration_ms / 1000
        return f"{seconds:.1f}s"
    else:
        minutes = duration_ms // 60000
        remaining_seconds = (duration_ms % 60000) // 1000
        return f"{minutes}m {remaining_seconds}s"


async def check_worker_status_async(job_id: str | None = None) -> str:
    """Check the status of a specific worker or list all active workers.

    Args:
        job_id: Optional worker job ID. If None, lists all active (queued/running) workers.

    Returns:
        Status information about the specified worker or list of active workers.
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot check worker status - no credential context available"

    db = resolver.db

    try:
        if job_id is not None:
            # Check specific job
            job_id_int = int(job_id)
            job = (
                db.query(crud.WorkerJob)
                .filter(
                    crud.WorkerJob.id == job_id_int,
                    crud.WorkerJob.owner_id == resolver.owner_id,
                )
                .first()
            )

            if not job:
                return f"Error: Worker job {job_id} not found"

            # Calculate elapsed time
            from zerg.utils.time import utc_now_naive

            elapsed_str = "N/A"
            if job.started_at:
                elapsed = (utc_now_naive() - job.started_at).total_seconds()
                if elapsed >= 3600:
                    elapsed_str = f"{int(elapsed / 3600)}h {int((elapsed % 3600) / 60)}m"
                elif elapsed >= 60:
                    elapsed_str = f"{int(elapsed / 60)}m {int(elapsed % 60)}s"
                else:
                    elapsed_str = f"{int(elapsed)}s"

            # Build status response
            lines = [
                f"Worker Job {job.id}:",
                f"  Status: {job.status.upper()}",
                f"  Task: {job.task[:100]}{'...' if len(job.task) > 100 else ''}",
                f"  Model: {job.model}",
                f"  Created: {job.created_at.isoformat() if job.created_at else 'N/A'}",
            ]

            if job.started_at:
                lines.append(f"  Started: {job.started_at.isoformat()}")
                lines.append(f"  Elapsed: {elapsed_str}")

            if job.finished_at:
                lines.append(f"  Finished: {job.finished_at.isoformat()}")

            if job.status in ["success", "failed"]:
                lines.append(f"\nUse read_commis_result({job.id}) to get the full result.")

            if job.error:
                lines.append(f"\nError: {job.error[:200]}{'...' if len(job.error) > 200 else ''}")

            return "\n".join(lines)
        else:
            # List all active workers
            active_jobs = (
                db.query(crud.WorkerJob)
                .filter(
                    crud.WorkerJob.owner_id == resolver.owner_id,
                    crud.WorkerJob.status.in_(["queued", "running"]),
                )
                .order_by(crud.WorkerJob.created_at.desc())
                .limit(20)
                .all()
            )

            if not active_jobs:
                return "No active workers. All workers have completed or there are none running."

            lines = [f"Active Workers ({len(active_jobs)}):\n"]
            for job in active_jobs:
                status_icon = "⏳" if job.status == "queued" else "⋯"
                task_preview = job.task[:60] + "..." if len(job.task) > 60 else job.task
                lines.append(f"- Job {job.id} [{status_icon} {job.status.upper()}]")
                lines.append(f"  {task_preview}\n")

            lines.append("Use check_commis_status(job_id) for details on a specific commis.")
            return "\n".join(lines)

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except Exception as e:
        logger.exception(f"Failed to check worker status: {job_id}")
        return f"Error checking worker status: {e}"


def check_worker_status(job_id: str | None = None) -> str:
    """Sync wrapper for check_worker_status_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(check_worker_status_async(job_id))


async def cancel_worker_async(job_id: str) -> str:
    """Cancel a running or queued worker job.

    Sets the job status to 'cancelled'. The worker process will check this
    status between tool iterations and abort if cancelled.

    Args:
        job_id: The worker job ID to cancel

    Returns:
        Confirmation message or error
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot cancel worker - no credential context available"

    db = resolver.db

    try:
        job_id_int = int(job_id)

        # Get job record
        job = (
            db.query(crud.WorkerJob)
            .filter(
                crud.WorkerJob.id == job_id_int,
                crud.WorkerJob.owner_id == resolver.owner_id,
            )
            .first()
        )

        if not job:
            return f"Error: Worker job {job_id} not found"

        if job.status in ["success", "failed", "cancelled"]:
            return f"Worker job {job_id} is already {job.status} and cannot be cancelled."

        # Update status to cancelled
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        job.error = "Cancelled by user"
        db.commit()

        logger.info(f"Worker job {job_id} cancelled by user")
        return f"Worker job {job_id} has been cancelled. It may take a moment for the worker to stop."

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except Exception as e:
        logger.exception(f"Failed to cancel worker: {job_id}")
        db.rollback()
        return f"Error cancelling worker: {e}"


def cancel_worker(job_id: str) -> str:
    """Sync wrapper for cancel_worker_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(cancel_worker_async(job_id))


async def wait_for_worker_async(
    job_id: str,
    *,
    _tool_call_id: str | None = None,
) -> str:
    """Wait for a specific worker to complete (blocking).

    This is an explicit opt-in to block execution until the worker completes.
    Use sparingly - the async inbox model is preferred for most cases.

    If the worker is still running, this raises AgentInterrupted to pause
    the supervisor until the worker completes.

    Args:
        job_id: The worker job ID to wait for

    Returns:
        The worker's result if already complete, or raises AgentInterrupted
    """
    from zerg.crud import crud
    from zerg.managers.agent_runner import AgentInterrupted

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot wait for worker - no credential context available"

    db = resolver.db

    try:
        job_id_int = int(job_id)

        # Get job record
        job = (
            db.query(crud.WorkerJob)
            .filter(
                crud.WorkerJob.id == job_id_int,
                crud.WorkerJob.owner_id == resolver.owner_id,
            )
            .first()
        )

        if not job:
            return f"Error: Worker job {job_id} not found"

        if job.status == "cancelled":
            return f"Worker job {job_id} was cancelled."

        if job.status == "failed":
            return f"Worker job {job_id} failed: {job.error or 'Unknown error'}"

        if job.status == "success":
            # Already complete - return result immediately
            if job.worker_id:
                try:
                    artifact_store = WorkerArtifactStore()
                    metadata = artifact_store.get_worker_metadata(job.worker_id)
                    summary = metadata.get("summary")
                    if summary:
                        return f"Worker job {job_id} completed:\n\n{summary}"
                    result = artifact_store.get_worker_result(job.worker_id)
                    return f"Worker job {job_id} completed:\n\n{result}"
                except FileNotFoundError:
                    return f"Worker job {job_id} completed but result not found."
            return f"Worker job {job_id} completed."

        # Job is still queued or running - raise interrupt to wait
        logger.info(f"[WAIT-FOR-WORKER] Blocking for job {job_id} (status: {job.status})")

        # Raise AgentInterrupted to pause supervisor
        raise AgentInterrupted(
            {
                "type": "wait_for_worker",
                "job_id": job_id_int,
                "task": job.task[:100],
                "tool_call_id": _tool_call_id,
                "message": f"Waiting for worker job {job_id} to complete...",
            }
        )

    except AgentInterrupted:
        # Re-raise interrupt
        raise
    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except Exception as e:
        logger.exception(f"Failed to wait for worker: {job_id}")
        return f"Error waiting for worker: {e}"


def wait_for_worker(job_id: str) -> str:
    """Sync wrapper for wait_for_worker_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(wait_for_worker_async(job_id))


async def read_worker_result_async(job_id: str) -> str:
    """Read the final result from a completed worker job.

    Args:
        job_id: The worker job ID (integer as string)

    Returns:
        The worker's natural language result with duration information
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot read worker result - no credential context available"

    db = resolver.db

    try:
        # Parse job ID
        job_id_int = int(job_id)

        # Get job record
        job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id_int, crud.WorkerJob.owner_id == resolver.owner_id).first()

        if not job:
            return f"Error: Worker job {job_id} not found"

        if not job.worker_id:
            return f"Error: Worker job {job_id} has not started execution yet"

        if job.status not in ["success", "failed"]:
            return f"Error: Worker job {job_id} is not complete (status: {job.status})"

        # Get result and metadata from artifacts
        artifact_store = WorkerArtifactStore()
        result = artifact_store.get_worker_result(job.worker_id)
        metadata = artifact_store.get_worker_metadata(job.worker_id, owner_id=resolver.owner_id)

        # Extract duration_ms from metadata
        duration_ms = metadata.get("duration_ms")
        duration_info = f"\n\nExecution time: {format_duration(duration_ms)}" if duration_ms is not None else ""

        return f"Result from worker job {job_id} (worker {job.worker_id}):{duration_info}\n\n{result}"

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except PermissionError:
        return f"Error: Access denied to worker job {job_id}"
    except FileNotFoundError:
        return f"Error: Worker job {job_id} not found or has no result yet"
    except Exception as e:
        logger.exception(f"Failed to read worker result: {job_id}")
        return f"Error reading worker result: {e}"


def read_worker_result(job_id: str) -> str:
    """Sync wrapper for read_worker_result_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(read_worker_result_async(job_id))


async def get_worker_evidence_async(job_id: str, budget_bytes: int = 32000) -> str:
    """Compile evidence for a worker job within a byte budget.

    This dereferences evidence markers like:
    [EVIDENCE:run_id=...,job_id=...,worker_id=...]

    Args:
        job_id: The worker job ID (integer as string)
        budget_bytes: Total byte budget for evidence (default 32KB)

    Returns:
        Evidence text compiled from worker artifacts
    """
    from zerg.crud import crud
    from zerg.services.evidence_compiler import EvidenceCompiler

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot fetch evidence - no credential context available"

    db = resolver.db

    # Clamp budget to reasonable limits
    safe_budget = max(1024, min(int(budget_bytes or 0), 200_000))

    try:
        job_id_int = int(job_id)
    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"

    try:
        job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id_int, crud.WorkerJob.owner_id == resolver.owner_id).first()
        if not job:
            return f"Error: Worker job {job_id} not found"

        if not job.worker_id:
            return f"Error: Worker job {job_id} has not started execution yet"

        compiler = EvidenceCompiler(db=db)
        evidence = compiler.compile_for_job(
            job_id=job.id,
            worker_id=job.worker_id,
            owner_id=resolver.owner_id,
            budget_bytes=safe_budget,
        )

        return f"Evidence for worker job {job_id} (worker {job.worker_id}, budget={safe_budget}B):\n\n{evidence}"

    except PermissionError:
        return f"Error: Access denied to worker job {job_id}"
    except Exception as e:
        logger.exception(f"Failed to compile evidence for worker job: {job_id}")
        return f"Error compiling evidence for worker job {job_id}: {e}"


def get_worker_evidence(job_id: str, budget_bytes: int = 32000) -> str:
    """Sync wrapper for get_worker_evidence_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(get_worker_evidence_async(job_id, budget_bytes))


def _truncate_head_tail(content: str, max_bytes: int, head_size: int = 1024) -> str:
    """Truncate content using head+tail strategy with marker.

    Reuses the truncation strategy from evidence_compiler:
    - First `head_size` bytes (default 1KB) always included
    - Remaining budget goes to tail
    - Marker indicates truncated bytes in the middle

    Args:
        content: Content to truncate
        max_bytes: Maximum bytes for output
        head_size: Size of head portion in bytes (default 1KB)

    Returns:
        Truncated content with marker if needed
    """
    content_bytes = content.encode("utf-8")
    total_bytes = len(content_bytes)

    if total_bytes <= max_bytes:
        return content

    # Reserve space for truncation marker (approximate)
    marker_template = "\n[...truncated {truncated_bytes} bytes...]\n"
    marker_estimate = marker_template.format(truncated_bytes=999999)
    marker_bytes = len(marker_estimate.encode("utf-8"))

    available = max_bytes - marker_bytes
    if available < head_size * 2:
        # Budget too small for head+tail, just return truncated head
        head_bytes = content_bytes[:max_bytes]
        return head_bytes.decode("utf-8", errors="replace") + "..."

    actual_head_size = min(head_size, available // 2)
    tail_size = available - actual_head_size

    head_bytes = content_bytes[:actual_head_size]
    tail_bytes = content_bytes[-tail_size:]

    head = head_bytes.decode("utf-8", errors="replace")
    tail = tail_bytes.decode("utf-8", errors="replace")

    truncated_bytes = total_bytes - actual_head_size - tail_size
    marker = marker_template.format(truncated_bytes=truncated_bytes)

    return f"{head}{marker}{tail}"


async def get_tool_output_async(artifact_id: str, max_bytes: int = 32000) -> str:
    """Fetch a stored tool output by artifact_id.

    Use this to dereference markers like:
    [TOOL_OUTPUT:artifact_id=...,tool=...,bytes=...]

    Args:
        artifact_id: The artifact ID from the tool output marker
        max_bytes: Maximum bytes to return (default 32KB, 0 for unlimited)
    """
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot fetch tool output - no credential context available"

    try:
        store = ToolOutputStore()
        content = store.read_output(owner_id=resolver.owner_id, artifact_id=artifact_id)

        metadata = None
        try:
            metadata = store.read_metadata(owner_id=resolver.owner_id, artifact_id=artifact_id)
        except FileNotFoundError:
            metadata = None

        header_parts: list[str] = []
        if metadata:
            tool_name = metadata.get("tool_name")
            if tool_name:
                header_parts.append(f"tool={tool_name}")
            run_id = metadata.get("run_id")
            if run_id is not None:
                header_parts.append(f"run_id={run_id}")
            tool_call_id = metadata.get("tool_call_id")
            if tool_call_id:
                header_parts.append(f"tool_call_id={tool_call_id}")
            size_bytes = metadata.get("size_bytes")
            if size_bytes is not None:
                header_parts.append(f"bytes={size_bytes}")

        header = f"Tool output {artifact_id}"
        if header_parts:
            header = f"{header} ({', '.join(header_parts)})"

        # Apply truncation if max_bytes > 0
        if max_bytes > 0:
            content = _truncate_head_tail(content, max_bytes)

        return f"{header}:\n\n{content}"

    except ValueError:
        return f"Error: Invalid artifact_id: {artifact_id}"
    except FileNotFoundError:
        return f"Error: Tool output {artifact_id} not found"
    except Exception as e:
        logger.exception("Failed to read tool output: %s", artifact_id)
        return f"Error reading tool output {artifact_id}: {e}"


def get_tool_output(artifact_id: str, max_bytes: int = 32000) -> str:
    """Sync wrapper for get_tool_output_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(get_tool_output_async(artifact_id, max_bytes))


async def read_worker_file_async(job_id: str, file_path: str) -> str:
    """Read a specific file from a worker job's artifacts.

    Use this to drill into worker details like tool outputs or full conversation.

    Args:
        job_id: The worker job ID (integer as string)
        file_path: Relative path within worker directory (e.g., "tool_calls/001_ssh_exec.txt")

    Returns:
        Contents of the file

    Common paths:
        - "result.txt" - Final result
        - "metadata.json" - Worker metadata (status, timestamps, config)
        - "thread.jsonl" - Full conversation history
        - "tool_calls/*.txt" - Individual tool outputs
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot read worker file - no credential context available"

    db = resolver.db

    try:
        # Parse job ID
        job_id_int = int(job_id)

        # Get job record
        job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id_int, crud.WorkerJob.owner_id == resolver.owner_id).first()

        if not job:
            return f"Error: Worker job {job_id} not found"

        if not job.worker_id:
            return f"Error: Worker job {job_id} has not started execution yet"

        # Read file from artifacts
        artifact_store = WorkerArtifactStore()
        # Verify access by checking metadata first
        artifact_store.get_worker_metadata(job.worker_id, owner_id=resolver.owner_id)

        content = artifact_store.read_worker_file(job.worker_id, file_path)
        return f"Contents of {file_path} from worker job {job_id} (worker {job.worker_id}):\n\n{content}"

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except PermissionError:
        return f"Error: Access denied to worker job {job_id}"
    except FileNotFoundError:
        return f"Error: File {file_path} not found in worker job {job_id}"
    except ValueError as e:
        return f"Error: Invalid file path - {e}"
    except Exception as e:
        logger.exception(f"Failed to read worker file: {job_id}/{file_path}")
        return f"Error reading worker file: {e}"


def read_worker_file(job_id: str, file_path: str) -> str:
    """Sync wrapper for read_worker_file_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(read_worker_file_async(job_id, file_path))


async def grep_workers_async(pattern: str, since_hours: int = 24) -> str:
    """Search across worker job artifacts for a pattern.

    Args:
        pattern: Text pattern to search for (case-insensitive)
        since_hours: Only search jobs from the last N hours (default: 24)

    Returns:
        Matches with job IDs and context
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot grep workers - no credential context available"

    db = resolver.db
    artifact_store = WorkerArtifactStore()

    try:
        # Get completed jobs with worker_ids
        query = db.query(crud.WorkerJob).filter(
            crud.WorkerJob.owner_id == resolver.owner_id,
            crud.WorkerJob.worker_id.isnot(None),
            crud.WorkerJob.status.in_(["success", "failed"]),
        )

        if since_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.WorkerJob.created_at >= cutoff)

        jobs = query.all()

        # Use case-insensitive regex
        import re

        case_insensitive_pattern = f"(?i){re.escape(pattern)}"

        # Search across artifacts for each job
        all_matches = []
        for job in jobs:
            try:
                matches = artifact_store.search_workers(
                    pattern=case_insensitive_pattern,
                    file_glob="**/*.txt",
                    worker_ids=[job.worker_id],  # Only search this worker
                )
                # Add job_id to each match
                for match in matches:
                    match["job_id"] = job.id
                all_matches.extend(matches)
            except Exception as e:
                logger.warning(f"Failed to search worker {job.worker_id}: {e}")
                continue

        if not all_matches:
            return f"No matches found for pattern '{pattern}' in last {since_hours} hours"

        # Format results
        lines = [f"Found {len(all_matches)} match(es) for '{pattern}':\n"]
        for match in all_matches[:50]:  # Limit to 50 matches
            job_id = match.get("job_id", "unknown")
            worker_id = match.get("worker_id", "unknown")
            file_name = match.get("file", "unknown")
            line_num = match.get("line", 0)
            content = match.get("content", "")

            lines.append(f"\nJob {job_id} (worker {worker_id})/{file_name}:{line_num}\n  {content[:200]}")

        if len(all_matches) > 50:
            lines.append(f"\n... and {len(all_matches) - 50} more matches (truncated)")

        return "\n".join(lines)

    except Exception as e:
        logger.exception(f"Failed to grep workers: {pattern}")
        return f"Error searching workers: {e}"


def grep_workers(pattern: str, since_hours: int = 24) -> str:
    """Sync wrapper for grep_workers_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(grep_workers_async(pattern, since_hours))


async def get_worker_metadata_async(job_id: str) -> str:
    """Get detailed metadata about a worker job execution.

    Args:
        job_id: The worker job ID (integer as string)

    Returns:
        Formatted metadata including task, status, timestamps, duration, config
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot get worker metadata - no credential context available"

    db = resolver.db

    try:
        # Parse job ID
        job_id_int = int(job_id)

        # Get job record
        job = db.query(crud.WorkerJob).filter(crud.WorkerJob.id == job_id_int, crud.WorkerJob.owner_id == resolver.owner_id).first()

        if not job:
            return f"Error: Worker job {job_id} not found"

        # Format nicely
        lines = [
            f"Metadata for worker job {job_id}:\n",
            f"Status: {job.status}",
            f"Task: {job.task}",
            f"Model: {job.model}",
            "\nTimestamps:",
            f"  Created: {job.created_at.isoformat() if job.created_at else 'N/A'}",
            f"  Started: {job.started_at.isoformat() if job.started_at else 'N/A'}",
            f"  Finished: {job.finished_at.isoformat() if job.finished_at else 'N/A'}",
        ]

        # Calculate duration
        from zerg.utils.time import utc_now_naive

        duration_str = "N/A"
        if job.started_at and job.finished_at:
            duration = (job.finished_at - job.started_at).total_seconds() * 1000
            duration_str = f"{int(duration)}ms"
        elif job.started_at and job.status == "running":
            duration = (utc_now_naive() - job.started_at).total_seconds() * 1000
            duration_str = f"{int(duration)}ms (running)"

        lines.append(f"  Duration: {duration_str}")

        if job.worker_id:
            lines.append(f"\nWorker ID: {job.worker_id}")

        # Add error if present
        if job.error:
            lines.append(f"\nError: {job.error}")

        return "\n".join(lines)

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except Exception as e:
        logger.exception(f"Failed to get worker metadata: {job_id}")
        return f"Error getting worker metadata: {e}"


def get_worker_metadata(job_id: str) -> str:
    """Sync wrapper for get_worker_metadata_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(get_worker_metadata_async(job_id))


# ---------------------------------------------------------------------------
# Tool registration and exports
# ---------------------------------------------------------------------------

# Note: We provide both func (sync) and coroutine (async) so LangChain
# can use whichever invocation method is appropriate for the runtime.
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=spawn_worker,
        coroutine=spawn_standard_worker_async,
        name="spawn_commis",
        description="Spawn a commis for server tasks, research, or investigations. "
        "Commis can run commands on servers via runner_exec. "
        "For repository/code tasks, use spawn_workspace_commis instead.",
    ),
    StructuredTool.from_function(
        func=spawn_workspace_worker,
        coroutine=spawn_workspace_worker_async,
        name="spawn_workspace_commis",
        description="Spawn a commis to work in a git repository. "
        "Clones the repo, runs the agent in an isolated workspace, and captures any changes. "
        "Use this for: reading code, analyzing dependencies, making changes, running tests.",
    ),
    StructuredTool.from_function(
        func=list_workers,
        coroutine=list_workers_async,
        name="list_commis",
        description="List recent commis jobs with SUMMARIES ONLY. "
        "Returns compressed summaries for quick scanning. "
        "Use read_commis_result(job_id) to get full details. "
        "This prevents context overflow when scanning 50+ commis.",
    ),
    StructuredTool.from_function(
        func=read_worker_result,
        coroutine=read_worker_result_async,
        name="read_commis_result",
        description="Read the final result from a completed commis job. "
        "Provide the job ID (integer) to get the natural language result text.",
    ),
    StructuredTool.from_function(
        func=get_worker_evidence,
        coroutine=get_worker_evidence_async,
        name="get_commis_evidence",
        description="Compile raw tool evidence for a commis job within a byte budget. "
        "Use this to dereference [EVIDENCE:...] markers when you need full artifact details.",
    ),
    StructuredTool.from_function(
        func=get_tool_output,
        coroutine=get_tool_output_async,
        name="get_tool_output",
        description="Fetch a stored tool output by artifact_id. "
        "Use this to dereference [TOOL_OUTPUT:...] markers. "
        "Returns truncated output by default (max_bytes=32KB). Pass max_bytes=0 for full content.",
    ),
    StructuredTool.from_function(
        func=read_worker_file,
        coroutine=read_worker_file_async,
        name="read_commis_file",
        description="Read a specific file from a commis job's artifacts. "
        "Provide the job ID (integer) and file path to drill into commis details like "
        "tool outputs (tool_calls/*.txt), conversation history (thread.jsonl), or metadata (metadata.json).",
    ),
    StructuredTool.from_function(
        func=grep_workers,
        coroutine=grep_workers_async,
        name="grep_commis",
        description="Search across completed commis job artifacts for a text pattern. "
        "Performs case-insensitive search and returns matches with job IDs and context. "
        "Useful for finding jobs that encountered specific errors or outputs.",
    ),
    StructuredTool.from_function(
        func=get_worker_metadata,
        coroutine=get_worker_metadata_async,
        name="get_commis_metadata",
        description="Get detailed metadata about a commis job execution including "
        "task, status, timestamps, duration, and configuration. "
        "Provide the job ID (integer) to inspect job details.",
    ),
    # Async inbox model tools
    StructuredTool.from_function(
        func=check_worker_status,
        coroutine=check_worker_status_async,
        name="check_commis_status",
        description="Check the status of a specific commis or list all active commis. "
        "Pass job_id for a specific commis, or call without arguments to see all active commis. "
        "Use this to monitor background commis without blocking.",
    ),
    StructuredTool.from_function(
        func=cancel_worker,
        coroutine=cancel_worker_async,
        name="cancel_commis",
        description="Cancel a running or queued commis job. "
        "The commis will abort at its next checkpoint. "
        "Use when a task is no longer needed or taking too long.",
    ),
    StructuredTool.from_function(
        func=wait_for_worker,
        coroutine=wait_for_worker_async,
        name="wait_for_commis",
        description="Wait for a specific commis to complete (blocking). "
        "Use sparingly - the async model is preferred. "
        "Only use when you need the result before proceeding.",
    ),
]

# ---------------------------------------------------------------------------
# Single source of truth for supervisor tool names
# ---------------------------------------------------------------------------

# Tool names derived from TOOLS list - this is the canonical source
SUPERVISOR_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOLS)

# Additional utility tools that supervisors need access to.
# These are NOT supervisor-specific but are commonly used by the supervisor agent.
# Organized by category for clarity.
SUPERVISOR_UTILITY_TOOLS: frozenset[str] = frozenset(
    [
        # Time/scheduling
        "get_current_time",
        # Web/HTTP
        "http_request",
        "web_search",
        "web_fetch",
        # Infrastructure
        "runner_list",
        "runner_create_enroll_token",
        # Communication
        "send_email",
        # Knowledge
        "knowledge_search",
        # Personal context (v2.1 Phase 4)
        "get_current_location",
        "get_whoop_data",
        "search_notes",
    ]
)


def get_supervisor_allowed_tools() -> list[str]:
    """Get the complete list of tools a supervisor agent should have access to.

    This is the SINGLE SOURCE OF TRUTH for supervisor tool allowlists.
    Used by supervisor_service.py when creating/updating supervisor agents.

    Returns:
        Sorted list of tool names (supervisor tools + utility tools)
    """
    return sorted(SUPERVISOR_TOOL_NAMES | SUPERVISOR_UTILITY_TOOLS)


# ---------------------------------------------------------------------------
# Aliases for new terminology (Phase 1 migration)
# These allow code to import with new names while keeping internal names stable
# ---------------------------------------------------------------------------
spawn_commis_async = spawn_standard_worker_async
spawn_workspace_commis_async = spawn_workspace_worker_async
wait_for_commis_async = wait_for_worker_async
