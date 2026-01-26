"""Concierge tools for spawning and managing commis fiches.

This module provides tools that allow concierge fiches to delegate tasks to
disposable commis fiches, retrieve their results, and drill into their artifacts.

The concierge/commis pattern enables complex delegation scenarios where a concierge
can spawn multiple commis for parallel execution or break down complex tasks.

Commis execution flow:
- spawn_commis() creates CommisJob and returns job info
- Caller (concierge_react_engine) raises CourseInterrupted to pause
- Commis runs in background via CommisJobProcessor
- Commis completion triggers resume via commis_resume.py
- Concierge resumes with commis result injected as tool response
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.models_config import DEFAULT_COMMIS_MODEL_ID
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.concierge_context import get_concierge_context
from zerg.services.tool_output_store import ToolOutputStore

logger = logging.getLogger(__name__)


async def spawn_commis_async(
    task: str,
    model: str | None = None,
    execution_mode: str = "standard",
    git_repo: str | None = None,
    resume_session_id: str | None = None,
    *,
    _tool_call_id: str | None = None,  # Internal: passed by _call_tool_async for idempotency
    _skip_interrupt: bool = False,  # Internal: caller handles interrupt
    _return_structured: bool = False,  # Internal: return dict instead of string for concierge_react_engine
) -> str | dict:
    """Spawn a commis fiche to execute a task and wait for completion.

    The commis runs in the background. Creates a CommisJob and returns job info
    for the caller to handle interruption and resumption.

    Args:
        task: Natural language description of what the commis should do
        model: LLM model for the commis (default: gpt-5-mini)
        execution_mode: "standard" (default) runs via WebSocket runner, "workspace"
            runs headless on the server in a git workspace.
        git_repo: Git repository URL (required if execution_mode="workspace").
            The repo is cloned, fiche makes changes, and diff is captured.
        resume_session_id: Life Hub session UUID to resume (workspace mode only).
            Enables cross-environment session continuity.

    Returns:
        The commis's result after completion

    Example:
        spawn_commis("Check disk usage on prod-web server via SSH")
        spawn_commis("Research vacuums and recommend the best one")
        spawn_commis("Fix typo in README", execution_mode="workspace", git_repo="git@github.com:user/repo.git")
        spawn_commis("Continue work", execution_mode="workspace", git_repo="...", resume_session_id="abc-123")
    """
    from zerg.models.models import CommisJob

    # Validate execution_mode and git_repo combination
    valid_modes = {"standard", "workspace"}
    if execution_mode not in valid_modes:
        return f"Error: execution_mode must be 'standard' or 'workspace', got '{execution_mode}'"

    # Workspace mode requires git_repo
    if execution_mode == "workspace" and not git_repo:
        return "Error: git_repo is required when execution_mode='workspace'"

    # Get database session from credential resolver context
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot spawn commis - no credential context available"

    db = resolver.db
    owner_id = resolver.owner_id

    # Get concierge course_id and trace_id from context (for SSE event correlation and debugging)
    ctx = get_concierge_context()
    concierge_course_id = ctx.course_id if ctx else None
    trace_id = ctx.trace_id if ctx else None

    # Commis inherits model and reasoning_effort from concierge context
    # Priority: explicit arg > concierge context > default
    commis_model = model or (ctx.model if ctx else None) or DEFAULT_COMMIS_MODEL_ID
    commis_reasoning_effort = (ctx.reasoning_effort if ctx else None) or "none"

    # Build execution config for workspace mode
    job_config = None
    if execution_mode == "workspace":
        job_config = {
            "execution_mode": "workspace",
            "git_repo": git_repo,
        }
        if resume_session_id:
            job_config["resume_session_id"] = resume_session_id

    try:
        # IDEMPOTENCY: Prevent duplicate commis on retry/resume.
        #
        # Primary strategy: Use tool_call_id (unique per LLM response) for exact idempotency.
        # Fallback strategy: Prefix matching on task string (handles cases without tool_call_id).
        #
        # This prevents duplicate commis while allowing legitimate multi-commis scenarios.

        commis_job = None
        existing_job = None

        # PRIMARY: Check for existing job with same tool_call_id (most reliable)
        if _tool_call_id and concierge_course_id:
            existing_job = (
                db.query(CommisJob)
                .filter(
                    CommisJob.concierge_course_id == concierge_course_id,
                    CommisJob.tool_call_id == _tool_call_id,
                )
                .first()
            )
            if existing_job:
                logger.info(f"[IDEMPOTENT] Found existing job {existing_job.id} for tool_call_id={_tool_call_id}")

        # FALLBACK: Check for completed/in-progress commis using task matching
        # (only if tool_call_id lookup didn't find anything)
        if existing_job is None:
            completed_jobs = (
                db.query(CommisJob)
                .filter(
                    CommisJob.concierge_course_id == concierge_course_id,
                    CommisJob.owner_id == owner_id,
                    CommisJob.status == "success",
                )
                .order_by(CommisJob.created_at.desc())
                .limit(20)
                .all()
            )

            if completed_jobs:
                # Exact task match only - prefix matching was removed as unsafe
                # (near-matches could return wrong commis results if tasks share prefixes)
                for job in completed_jobs:
                    if job.task == task:
                        existing_job = job
                        break

        if existing_job is None:
            # No completed match - check for in-progress job with EXACT task match
            existing_job = (
                db.query(CommisJob)
                .filter(
                    CommisJob.concierge_course_id == concierge_course_id,
                    CommisJob.task == task,
                    CommisJob.owner_id == owner_id,
                    CommisJob.status.in_(["queued", "running"]),
                )
                .first()
            )

        if existing_job:
            if existing_job.status == "success":
                # Already completed - return cached result immediately
                # This prevents duplicate commis on retry
                logger.debug(f"Existing job {existing_job.id} already succeeded, returning cached result")
                if existing_job.commis_id:
                    try:
                        artifact_store = CommisArtifactStore()
                        # Use summary-first approach (consistent with resume path)
                        metadata = artifact_store.get_commis_metadata(existing_job.commis_id)
                        summary = metadata.get("summary")
                        if summary:
                            return f"Commis job {existing_job.id} completed:\n\n{summary}"
                        # Fall back to full result if no summary
                        result = artifact_store.get_commis_result(existing_job.commis_id)
                        return f"Commis job {existing_job.id} completed:\n\n{result}"
                    except FileNotFoundError:
                        # Result artifact not available, treat as if job doesn't exist
                        logger.warning(f"Job {existing_job.id} SUCCESS but no result artifact, creating new job")
                else:
                    logger.warning(f"Job {existing_job.id} SUCCESS but no commis_id, creating new job")
                # Fall through to create new job
            else:
                # queued or running - reuse and wait via interrupt
                commis_job = existing_job
                logger.debug(f"Reusing existing commis job {commis_job.id} (status: {existing_job.status})")

        if commis_job is None:
            # Create new commis job record with tool_call_id for idempotency
            import uuid as uuid_module

            commis_job = CommisJob(
                owner_id=owner_id,
                concierge_course_id=concierge_course_id,
                tool_call_id=_tool_call_id,  # Enables idempotency on retry/resume
                trace_id=uuid_module.UUID(trace_id) if trace_id else None,  # Inherit from concierge for debugging
                task=task,
                model=commis_model,
                reasoning_effort=commis_reasoning_effort,  # Inherit from concierge
                status="queued",
                config=job_config,  # Cloud execution config (execution_mode, git_repo)
            )
            db.add(commis_job)
            db.commit()
            db.refresh(commis_job)
            logger.info(f"[SPAWN] Created commis job {commis_job.id} with tool_call_id={_tool_call_id}")

            # Emit COMMIS_SPAWNED event durably (replays on reconnect)
            # Only persist if we have a concierge course_id (test mocks may not have one)
            if concierge_course_id is not None:
                from zerg.services.event_store import append_course_event

                await append_course_event(
                    course_id=concierge_course_id,
                    event_type="commis_spawned",
                    payload={
                        "job_id": commis_job.id,
                        "tool_call_id": _tool_call_id,
                        "task": task[:100],
                        "model": commis_model,
                        "owner_id": owner_id,
                        "trace_id": trace_id,
                    },
                )

        # Return job info for caller to handle interruption.
        # concierge_react_engine raises CourseInterrupted
        # to pause execution until commis completes.
        logger.debug(f"spawn_commis returning queued response for job {commis_job.id}")
        if _return_structured:
            return {"job_id": commis_job.id, "status": "queued", "task": task[:100]}
        return f"Commis job {commis_job.id} queued successfully. Working on: {task[:100]}"

    except Exception as e:
        logger.exception(f"Failed to spawn commis for task: {task}")
        db.rollback()
        return f"Error spawning commis: {e}"


async def spawn_standard_commis_async(
    task: str,
    model: str | None = None,
    *,
    _tool_call_id: str | None = None,
    _return_structured: bool = False,
) -> str | dict:
    """Spawn a commis for general tasks (server commands, research, etc).

    For repository/code tasks, use spawn_workspace_commis instead.

    Args:
        task: Natural language description of what the commis should do
        model: LLM model for the commis (optional)

    Returns:
        The commis's result after completion
    """
    return await spawn_commis_async(
        task=task,
        model=model,
        execution_mode="standard",
        git_repo=None,
        _tool_call_id=_tool_call_id,
        _return_structured=_return_structured,
    )


def spawn_commis(
    task: str,
    model: str | None = None,
) -> str:
    """Spawn a commis for general tasks (server commands, research, etc).

    For repository/code tasks, use spawn_workspace_commis instead.
    """
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(spawn_standard_commis_async(task, model))


async def spawn_workspace_commis_async(
    task: str,
    git_repo: str,
    model: str | None = None,
    resume_session_id: str | None = None,
    *,
    _tool_call_id: str | None = None,
    _return_structured: bool = False,
) -> str | dict:
    """Spawn a commis to execute a task in a git repository workspace.

    The repository is cloned to an isolated workspace, the courses
    headlessly, and any changes are captured as a diff.

    Args:
        task: What to do in the repository (analyze code, fix bug, etc)
        git_repo: Repository URL (https://github.com/org/repo.git or git@github.com:org/repo.git)
        model: LLM model for the commis (optional)
        resume_session_id: Life Hub session UUID to resume (for session continuity)

    Returns:
        The commis's result after completion

    Example:
        spawn_workspace_commis("List dependencies from pyproject.toml", "https://github.com/langchain-ai/langchain.git")
        spawn_workspace_commis("Fix the typo in README.md", "git@github.com:user/repo.git")
        spawn_workspace_commis("Continue the work", "git@...", resume_session_id="abc-123")
    """
    # Early validation: reject dangerous URLs before job creation (defense in depth)
    # Delegate to shared validator to stay consistent with workspace_manager rules.
    from zerg.services.workspace_manager import validate_git_repo_url

    try:
        validate_git_repo_url(git_repo)
    except ValueError as exc:
        return f"Error: {exc}"

    # Delegate to the core implementation with workspace mode forced
    return await spawn_commis_async(
        task=task,
        model=model,
        execution_mode="workspace",
        git_repo=git_repo,
        resume_session_id=resume_session_id,
        _tool_call_id=_tool_call_id,
        _return_structured=_return_structured,
    )


def spawn_workspace_commis(
    task: str,
    git_repo: str,
    model: str | None = None,
    resume_session_id: str | None = None,
) -> str:
    """Sync wrapper for spawn_workspace_commis_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(spawn_workspace_commis_async(task, git_repo, model, resume_session_id))


async def list_commis_async(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """List recent commis jobs with SUMMARIES ONLY.

    Returns compressed summaries for scanning. To get full details,
    call read_commis_result(job_id).

    This prevents context overflow when scanning 50+ commis.

    Args:
        limit: Maximum number of jobs to return (default: 20)
        status: Filter by status ("queued", "running", "success", "failed", or None for all)
        since_hours: Only show jobs from the last N hours

    Returns:
        Formatted list of commis jobs with summaries (not full results)
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot list commis - no credential context available"

    db = resolver.db

    try:
        # Query commis jobs with filtering
        query = db.query(crud.CommisJob).filter(crud.CommisJob.owner_id == resolver.owner_id)

        if status:
            query = query.filter(crud.CommisJob.status == status)

        if since_hours is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.CommisJob.created_at >= since)

        jobs = query.order_by(crud.CommisJob.created_at.desc()).limit(limit).all()

        if not jobs:
            return "No commis jobs found matching criteria."

        # Get artifact store for summary lookup
        artifact_store = CommisArtifactStore()

        # Format output - compact with summaries
        lines = [f"Recent commis (showing {len(jobs)}):\n"]
        for job in jobs:
            job_id = job.id
            job_status = job.status

            # Get summary from artifact store if available, else truncate task
            summary = None
            if job.commis_id and job.status in ["success", "failed"]:
                try:
                    metadata = artifact_store.get_commis_metadata(job.commis_id)
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
        logger.exception("Failed to list commis jobs")
        return f"Error listing commis jobs: {e}"


def list_commis(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """Sync wrapper for list_commis_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(list_commis_async(limit, status, since_hours))


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


async def check_commis_status_async(job_id: str | None = None) -> str:
    """Check the status of a specific commis or list all active commis.

    Args:
        job_id: Optional commis job ID. If None, lists all active (queued/running) commis.

    Returns:
        Status information about the specified commis or list of active commis.
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot check commis status - no credential context available"

    db = resolver.db

    try:
        if job_id is not None:
            # Check specific job
            job_id_int = int(job_id)
            job = (
                db.query(crud.CommisJob)
                .filter(
                    crud.CommisJob.id == job_id_int,
                    crud.CommisJob.owner_id == resolver.owner_id,
                )
                .first()
            )

            if not job:
                return f"Error: Commis job {job_id} not found"

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
                f"Commis Job {job.id}:",
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
            # List all active commis
            active_jobs = (
                db.query(crud.CommisJob)
                .filter(
                    crud.CommisJob.owner_id == resolver.owner_id,
                    crud.CommisJob.status.in_(["queued", "running"]),
                )
                .order_by(crud.CommisJob.created_at.desc())
                .limit(20)
                .all()
            )

            if not active_jobs:
                return "No active commis. All commis have completed or there are none running."

            lines = [f"Active Commis ({len(active_jobs)}):\n"]
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
        logger.exception(f"Failed to check commis status: {job_id}")
        return f"Error checking commis status: {e}"


def check_commis_status(job_id: str | None = None) -> str:
    """Sync wrapper for check_commis_status_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(check_commis_status_async(job_id))


async def cancel_commis_async(job_id: str) -> str:
    """Cancel a running or queued commis job.

    Sets the job status to 'cancelled'. The commis process will check this
    status between tool iterations and abort if cancelled.

    Args:
        job_id: The commis job ID to cancel

    Returns:
        Confirmation message or error
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot cancel commis - no credential context available"

    db = resolver.db

    try:
        job_id_int = int(job_id)

        # Get job record
        job = (
            db.query(crud.CommisJob)
            .filter(
                crud.CommisJob.id == job_id_int,
                crud.CommisJob.owner_id == resolver.owner_id,
            )
            .first()
        )

        if not job:
            return f"Error: Commis job {job_id} not found"

        if job.status in ["success", "failed", "cancelled"]:
            return f"Commis job {job_id} is already {job.status} and cannot be cancelled."

        # Update status to cancelled
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        job.error = "Cancelled by user"
        db.commit()

        logger.info(f"Commis job {job_id} cancelled by user")
        return f"Commis job {job_id} has been cancelled. It may take a moment for the commis to stop."

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except Exception as e:
        logger.exception(f"Failed to cancel commis: {job_id}")
        db.rollback()
        return f"Error cancelling commis: {e}"


def cancel_commis(job_id: str) -> str:
    """Sync wrapper for cancel_commis_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(cancel_commis_async(job_id))


async def wait_for_commis_async(
    job_id: str,
    *,
    _tool_call_id: str | None = None,
) -> str:
    """Wait for a specific commis to complete (blocking).

    This is an explicit opt-in to block execution until the commis completes.
    Use sparingly - the async inbox model is preferred for most cases.

    If the commis is still running, this raises CourseInterrupted to pause
    the concierge until the commis completes.

    Args:
        job_id: The commis job ID to wait for

    Returns:
        The commis's result if already complete, or raises CourseInterrupted
    """
    from zerg.crud import crud
    from zerg.managers.fiche_runner import CourseInterrupted

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot wait for commis - no credential context available"

    db = resolver.db

    try:
        job_id_int = int(job_id)

        # Get job record
        job = (
            db.query(crud.CommisJob)
            .filter(
                crud.CommisJob.id == job_id_int,
                crud.CommisJob.owner_id == resolver.owner_id,
            )
            .first()
        )

        if not job:
            return f"Error: Commis job {job_id} not found"

        if job.status == "cancelled":
            return f"Commis job {job_id} was cancelled."

        if job.status == "failed":
            return f"Commis job {job_id} failed: {job.error or 'Unknown error'}"

        if job.status == "success":
            # Already complete - return result immediately
            if job.commis_id:
                try:
                    artifact_store = CommisArtifactStore()
                    metadata = artifact_store.get_commis_metadata(job.commis_id)
                    summary = metadata.get("summary")
                    if summary:
                        return f"Commis job {job_id} completed:\n\n{summary}"
                    result = artifact_store.get_commis_result(job.commis_id)
                    return f"Commis job {job_id} completed:\n\n{result}"
                except FileNotFoundError:
                    return f"Commis job {job_id} completed but result not found."
            return f"Commis job {job_id} completed."

        # Job is still queued or running - raise interrupt to wait
        logger.info(f"[WAIT-FOR-COMMIS] Blocking for job {job_id} (status: {job.status})")

        # Raise CourseInterrupted to pause concierge
        raise CourseInterrupted(
            {
                "type": "wait_for_commis",
                "job_id": job_id_int,
                "task": job.task[:100],
                "tool_call_id": _tool_call_id,
                "message": f"Waiting for commis job {job_id} to complete...",
            }
        )

    except CourseInterrupted:
        # Re-raise interrupt
        raise
    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except Exception as e:
        logger.exception(f"Failed to wait for commis: {job_id}")
        return f"Error waiting for commis: {e}"


def wait_for_commis(job_id: str) -> str:
    """Sync wrapper for wait_for_commis_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(wait_for_commis_async(job_id))


async def read_commis_result_async(job_id: str) -> str:
    """Read the final result from a completed commis job.

    Args:
        job_id: The commis job ID (integer as string)

    Returns:
        The commis's natural language result with duration information
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot read commis result - no credential context available"

    db = resolver.db

    try:
        # Parse job ID
        job_id_int = int(job_id)

        # Get job record
        job = db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id_int, crud.CommisJob.owner_id == resolver.owner_id).first()

        if not job:
            return f"Error: Commis job {job_id} not found"

        if not job.commis_id:
            return f"Error: Commis job {job_id} has not started execution yet"

        if job.status not in ["success", "failed"]:
            return f"Error: Commis job {job_id} is not complete (status: {job.status})"

        # Get result and metadata from artifacts
        artifact_store = CommisArtifactStore()
        result = artifact_store.get_commis_result(job.commis_id)
        metadata = artifact_store.get_commis_metadata(job.commis_id, owner_id=resolver.owner_id)

        # Extract duration_ms from metadata
        duration_ms = metadata.get("duration_ms")
        duration_info = f"\n\nExecution time: {format_duration(duration_ms)}" if duration_ms is not None else ""

        return f"Result from commis job {job_id} (commis {job.commis_id}):{duration_info}\n\n{result}"

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except PermissionError:
        return f"Error: Access denied to commis job {job_id}"
    except FileNotFoundError:
        return f"Error: Commis job {job_id} not found or has no result yet"
    except Exception as e:
        logger.exception(f"Failed to read commis result: {job_id}")
        return f"Error reading commis result: {e}"


def read_commis_result(job_id: str) -> str:
    """Sync wrapper for read_commis_result_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(read_commis_result_async(job_id))


async def get_commis_evidence_async(job_id: str, budget_bytes: int = 32000) -> str:
    """Compile evidence for a commis job within a byte budget.

    This dereferences evidence markers like:
    [EVIDENCE:course_id=...,job_id=...,commis_id=...]

    Args:
        job_id: The commis job ID (integer as string)
        budget_bytes: Total byte budget for evidence (default 32KB)

    Returns:
        Evidence text compiled from commis artifacts
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
        job = db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id_int, crud.CommisJob.owner_id == resolver.owner_id).first()
        if not job:
            return f"Error: Commis job {job_id} not found"

        if not job.commis_id:
            return f"Error: Commis job {job_id} has not started execution yet"

        compiler = EvidenceCompiler(db=db)
        evidence = compiler.compile_for_job(
            job_id=job.id,
            commis_id=job.commis_id,
            owner_id=resolver.owner_id,
            budget_bytes=safe_budget,
        )

        return f"Evidence for commis job {job_id} (commis {job.commis_id}, budget={safe_budget}B):\n\n{evidence}"

    except PermissionError:
        return f"Error: Access denied to commis job {job_id}"
    except Exception as e:
        logger.exception(f"Failed to compile evidence for commis job: {job_id}")
        return f"Error compiling evidence for commis job {job_id}: {e}"


def get_commis_evidence(job_id: str, budget_bytes: int = 32000) -> str:
    """Sync wrapper for get_commis_evidence_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(get_commis_evidence_async(job_id, budget_bytes))


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
            course_id = metadata.get("course_id")
            if course_id is not None:
                header_parts.append(f"course_id={course_id}")
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


async def read_commis_file_async(job_id: str, file_path: str) -> str:
    """Read a specific file from a commis job's artifacts.

    Use this to drill into commis details like tool outputs or full conversation.

    Args:
        job_id: The commis job ID (integer as string)
        file_path: Relative path within commis directory (e.g., "tool_calls/001_ssh_exec.txt")

    Returns:
        Contents of the file

    Common paths:
        - "result.txt" - Final result
        - "metadata.json" - Commis metadata (status, timestamps, config)
        - "thread.jsonl" - Full conversation history
        - "tool_calls/*.txt" - Individual tool outputs
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot read commis file - no credential context available"

    db = resolver.db

    try:
        # Parse job ID
        job_id_int = int(job_id)

        # Get job record
        job = db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id_int, crud.CommisJob.owner_id == resolver.owner_id).first()

        if not job:
            return f"Error: Commis job {job_id} not found"

        if not job.commis_id:
            return f"Error: Commis job {job_id} has not started execution yet"

        # Read file from artifacts
        artifact_store = CommisArtifactStore()
        # Verify access by checking metadata first
        artifact_store.get_commis_metadata(job.commis_id, owner_id=resolver.owner_id)

        content = artifact_store.read_commis_file(job.commis_id, file_path)
        return f"Contents of {file_path} from commis job {job_id} (commis {job.commis_id}):\n\n{content}"

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except PermissionError:
        return f"Error: Access denied to commis job {job_id}"
    except FileNotFoundError:
        return f"Error: File {file_path} not found in commis job {job_id}"
    except ValueError as e:
        return f"Error: Invalid file path - {e}"
    except Exception as e:
        logger.exception(f"Failed to read commis file: {job_id}/{file_path}")
        return f"Error reading commis file: {e}"


def read_commis_file(job_id: str, file_path: str) -> str:
    """Sync wrapper for read_commis_file_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(read_commis_file_async(job_id, file_path))


async def grep_commis_async(pattern: str, since_hours: int = 24) -> str:
    """Search across commis job artifacts for a pattern.

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
        return "Error: Cannot grep commis - no credential context available"

    db = resolver.db
    artifact_store = CommisArtifactStore()

    try:
        # Get completed jobs with commis_ids
        query = db.query(crud.CommisJob).filter(
            crud.CommisJob.owner_id == resolver.owner_id,
            crud.CommisJob.commis_id.isnot(None),
            crud.CommisJob.status.in_(["success", "failed"]),
        )

        if since_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.CommisJob.created_at >= cutoff)

        jobs = query.all()

        # Use case-insensitive regex
        import re

        case_insensitive_pattern = f"(?i){re.escape(pattern)}"

        # Search across artifacts for each job
        all_matches = []
        for job in jobs:
            try:
                matches = artifact_store.search_commis(
                    pattern=case_insensitive_pattern,
                    file_glob="**/*.txt",
                    commis_ids=[job.commis_id],  # Only search this commis
                )
                # Add job_id to each match
                for match in matches:
                    match["job_id"] = job.id
                all_matches.extend(matches)
            except Exception as e:
                logger.warning(f"Failed to search commis {job.commis_id}: {e}")
                continue

        if not all_matches:
            return f"No matches found for pattern '{pattern}' in last {since_hours} hours"

        # Format results
        lines = [f"Found {len(all_matches)} match(es) for '{pattern}':\n"]
        for match in all_matches[:50]:  # Limit to 50 matches
            job_id = match.get("job_id", "unknown")
            commis_id = match.get("commis_id", "unknown")
            file_name = match.get("file", "unknown")
            line_num = match.get("line", 0)
            content = match.get("content", "")

            lines.append(f"\nJob {job_id} (commis {commis_id})/{file_name}:{line_num}\n  {content[:200]}")

        if len(all_matches) > 50:
            lines.append(f"\n... and {len(all_matches) - 50} more matches (truncated)")

        return "\n".join(lines)

    except Exception as e:
        logger.exception(f"Failed to grep commis: {pattern}")
        return f"Error searching commis: {e}"


def grep_commis(pattern: str, since_hours: int = 24) -> str:
    """Sync wrapper for grep_commis_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(grep_commis_async(pattern, since_hours))


async def get_commis_metadata_async(job_id: str) -> str:
    """Get detailed metadata about a commis job execution.

    Args:
        job_id: The commis job ID (integer as string)

    Returns:
        Formatted metadata including task, status, timestamps, duration, config
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot get commis metadata - no credential context available"

    db = resolver.db

    try:
        # Parse job ID
        job_id_int = int(job_id)

        # Get job record
        job = db.query(crud.CommisJob).filter(crud.CommisJob.id == job_id_int, crud.CommisJob.owner_id == resolver.owner_id).first()

        if not job:
            return f"Error: Commis job {job_id} not found"

        # Format nicely
        lines = [
            f"Metadata for commis job {job_id}:\n",
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

        if job.commis_id:
            lines.append(f"\nCommis ID: {job.commis_id}")

        # Add error if present
        if job.error:
            lines.append(f"\nError: {job.error}")

        return "\n".join(lines)

    except ValueError:
        return f"Error: Invalid job ID format: {job_id}"
    except Exception as e:
        logger.exception(f"Failed to get commis metadata: {job_id}")
        return f"Error getting commis metadata: {e}"


def get_commis_metadata(job_id: str) -> str:
    """Sync wrapper for get_commis_metadata_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(get_commis_metadata_async(job_id))


# ---------------------------------------------------------------------------
# Tool registration and exports
# ---------------------------------------------------------------------------

# Note: We provide both func (sync) and coroutine (async) so LangChain
# can use whichever invocation method is appropriate for the runtime.
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=spawn_commis,
        coroutine=spawn_standard_commis_async,
        name="spawn_commis",
        description="Spawn a commis for server tasks, research, or investigations. "
        "Commis can run commands on servers via runner_exec. "
        "For repository/code tasks, use spawn_workspace_commis instead.",
    ),
    StructuredTool.from_function(
        func=spawn_workspace_commis,
        coroutine=spawn_workspace_commis_async,
        name="spawn_workspace_commis",
        description="Spawn a commis to work in a git repository. "
        "Clones the repo, runs the fiche in an isolated workspace, and captures any changes. "
        "Use this for: reading code, analyzing dependencies, making changes, running tests.",
    ),
    StructuredTool.from_function(
        func=list_commis,
        coroutine=list_commis_async,
        name="list_commis",
        description="List recent commis jobs with SUMMARIES ONLY. "
        "Returns compressed summaries for quick scanning. "
        "Use read_commis_result(job_id) to get full details. "
        "This prevents context overflow when scanning 50+ commis.",
    ),
    StructuredTool.from_function(
        func=read_commis_result,
        coroutine=read_commis_result_async,
        name="read_commis_result",
        description="Read the final result from a completed commis job. "
        "Provide the job ID (integer) to get the natural language result text.",
    ),
    StructuredTool.from_function(
        func=get_commis_evidence,
        coroutine=get_commis_evidence_async,
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
        func=read_commis_file,
        coroutine=read_commis_file_async,
        name="read_commis_file",
        description="Read a specific file from a commis job's artifacts. "
        "Provide the job ID (integer) and file path to drill into commis details like "
        "tool outputs (tool_calls/*.txt), conversation history (thread.jsonl), or metadata (metadata.json).",
    ),
    StructuredTool.from_function(
        func=grep_commis,
        coroutine=grep_commis_async,
        name="grep_commis",
        description="Search across completed commis job artifacts for a text pattern. "
        "Performs case-insensitive search and returns matches with job IDs and context. "
        "Useful for finding jobs that encountered specific errors or outputs.",
    ),
    StructuredTool.from_function(
        func=get_commis_metadata,
        coroutine=get_commis_metadata_async,
        name="get_commis_metadata",
        description="Get detailed metadata about a commis job execution including "
        "task, status, timestamps, duration, and configuration. "
        "Provide the job ID (integer) to inspect job details.",
    ),
    # Async inbox model tools
    StructuredTool.from_function(
        func=check_commis_status,
        coroutine=check_commis_status_async,
        name="check_commis_status",
        description="Check the status of a specific commis or list all active commis. "
        "Pass job_id for a specific commis, or call without arguments to see all active commis. "
        "Use this to monitor background commis without blocking.",
    ),
    StructuredTool.from_function(
        func=cancel_commis,
        coroutine=cancel_commis_async,
        name="cancel_commis",
        description="Cancel a running or queued commis job. "
        "The commis will abort at its next checkpoint. "
        "Use when a task is no longer needed or taking too long.",
    ),
    StructuredTool.from_function(
        func=wait_for_commis,
        coroutine=wait_for_commis_async,
        name="wait_for_commis",
        description="Wait for a specific commis to complete (blocking). "
        "Use sparingly - the async model is preferred. "
        "Only use when you need the result before proceeding.",
    ),
]

# ---------------------------------------------------------------------------
# Single source of truth for concierge tool names
# ---------------------------------------------------------------------------

# Tool names derived from TOOLS list - this is the canonical source
CONCIERGE_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOLS)

# Additional utility tools that concierges need access to.
# These are NOT concierge-specific but are commonly used by the concierge fiche.
# Organized by category for clarity.
CONCIERGE_UTILITY_TOOLS: frozenset[str] = frozenset(
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


def get_concierge_allowed_tools() -> list[str]:
    """Get the complete list of tools a concierge fiche should have access to.

    This is the SINGLE SOURCE OF TRUTH for concierge tool allowlists.
    Used by concierge_service.py when creating/updating concierge fiches.

    Returns:
        Sorted list of tool names (concierge tools + utility tools)
    """
    return sorted(CONCIERGE_TOOL_NAMES | CONCIERGE_UTILITY_TOOLS)
