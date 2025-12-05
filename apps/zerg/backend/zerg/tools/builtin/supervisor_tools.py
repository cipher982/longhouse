"""Supervisor tools for spawning and managing worker agents.

This module provides tools that allow supervisor agents to delegate tasks to
disposable worker agents, retrieve their results, and drill into their artifacts.

The supervisor/worker pattern enables complex delegation scenarios where a supervisor
can spawn multiple workers for parallel execution or break down complex tasks.
"""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import List

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.models_config import DEFAULT_WORKER_MODEL_ID
from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)


async def spawn_worker_async(task: str, model: str | None = None) -> str:
    """Spawn a worker agent to execute a task.

    The worker runs independently, persists all outputs to disk, and returns
    a natural language result. Use this when you need to delegate work that
    might involve multiple tool calls or generate verbose output.

    Args:
        task: Natural language description of what the worker should do
        model: LLM model for the worker (default: gpt-5-mini)

    Returns:
        A summary indicating the job has been queued

    Example:
        spawn_worker("Check disk usage on cube server via SSH")
        spawn_worker("Research the top 5 robot vacuums under $500", model="gpt-5.1-2025-11-13")
    """
    from zerg.crud import crud
    from zerg.events import EventType, event_bus
    from zerg.models.models import WorkerJob
    from zerg.services.supervisor_context import get_supervisor_run_id

    # Get database session from credential resolver context
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot spawn worker - no credential context available"

    db = resolver.db
    owner_id = resolver.owner_id

    # Get supervisor run_id from context (for SSE event correlation)
    supervisor_run_id = get_supervisor_run_id()

    # Use default worker model if not specified
    worker_model = model or DEFAULT_WORKER_MODEL_ID

    # Create worker job record
    try:
        worker_job = WorkerJob(
            owner_id=owner_id,
            supervisor_run_id=supervisor_run_id,  # Correlate with supervisor run
            task=task,
            model=worker_model,
            status="queued"
        )
        db.add(worker_job)
        db.commit()
        db.refresh(worker_job)

        # Emit WORKER_SPAWNED event for SSE streaming
        # Include supervisor_run_id so SSE filter can pass it through
        await event_bus.publish(
            EventType.WORKER_SPAWNED,
            {
                "event_type": EventType.WORKER_SPAWNED,
                "job_id": worker_job.id,
                "task": task[:100],
                "model": worker_model,
                "owner_id": owner_id,
                "run_id": supervisor_run_id,  # For SSE correlation
            },
        )

        return (
            f"Worker job {worker_job.id} queued successfully.\n\n"
            f"Task: {task}\n"
            f"Model: {worker_model}\n\n"
            f"The worker will execute in the background. Use get_worker_metadata({worker_job.id}) "
            f"to check status and read_worker_result('{worker_job.id}') to get results when complete."
        )

    except Exception as e:
        logger.exception(f"Failed to queue worker job for task: {task}")
        db.rollback()
        return f"Error queuing worker job: {e}"


def spawn_worker(task: str, model: str | None = None) -> str:
    """Sync wrapper for spawn_worker_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely
    return run_async_safely(spawn_worker_async(task, model))


async def list_workers_async(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """List recent worker jobs with SUMMARIES ONLY.

    Returns compressed summaries for scanning. To get full details,
    call read_worker_result(job_id).

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

        lines.append("Use read_worker_result(job_id) for full details.")
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


async def read_worker_result_async(job_id: str) -> str:
    """Read the final result from a completed worker job.

    Args:
        job_id: The worker job ID (integer as string)

    Returns:
        The worker's natural language result
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
        job = db.query(crud.WorkerJob).filter(
            crud.WorkerJob.id == job_id_int,
            crud.WorkerJob.owner_id == resolver.owner_id
        ).first()

        if not job:
            return f"Error: Worker job {job_id} not found"

        if not job.worker_id:
            return f"Error: Worker job {job_id} has not started execution yet"

        if job.status not in ["success", "failed"]:
            return f"Error: Worker job {job_id} is not complete (status: {job.status})"

        # Get result from artifacts
        artifact_store = WorkerArtifactStore()
        result = artifact_store.get_worker_result(job.worker_id)
        return f"Result from worker job {job_id} (worker {job.worker_id}):\n\n{result}"

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
        job = db.query(crud.WorkerJob).filter(
            crud.WorkerJob.id == job_id_int,
            crud.WorkerJob.owner_id == resolver.owner_id
        ).first()

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
            crud.WorkerJob.status.in_(["success", "failed"])
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
                    worker_ids=[job.worker_id]  # Only search this worker
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

            lines.append(
                f"\nJob {job_id} (worker {worker_id})/{file_name}:{line_num}\n" f"  {content[:200]}"
            )

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
        job = db.query(crud.WorkerJob).filter(
            crud.WorkerJob.id == job_id_int,
            crud.WorkerJob.owner_id == resolver.owner_id
        ).first()

        if not job:
            return f"Error: Worker job {job_id} not found"

        # Format nicely
        lines = [
            f"Metadata for worker job {job_id}:\n",
            f"Status: {job.status}",
            f"Task: {job.task}",
            f"Model: {job.model}",
            f"\nTimestamps:",
            f"  Created: {job.created_at.isoformat() if job.created_at else 'N/A'}",
            f"  Started: {job.started_at.isoformat() if job.started_at else 'N/A'}",
            f"  Finished: {job.finished_at.isoformat() if job.finished_at else 'N/A'}",
        ]

        # Calculate duration
        duration_str = "N/A"
        if job.started_at and job.finished_at:
            duration = (job.finished_at - job.started_at).total_seconds() * 1000
            duration_str = f"{int(duration)}ms"
        elif job.started_at and job.status == "running":
            duration = (datetime.now(timezone.utc) - job.started_at).total_seconds() * 1000
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


# Export tools list for registration
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        coroutine=spawn_worker_async,
        name="spawn_worker",
        description="Spawn a worker agent to execute a task independently. "
        "The worker persists all outputs and returns a natural language result. "
        "Use for delegation, parallel work, or tasks that might generate verbose output.",
    ),
    StructuredTool.from_function(
        coroutine=list_workers_async,
        name="list_workers",
        description="List recent worker jobs with SUMMARIES ONLY. "
        "Returns compressed summaries for quick scanning. "
        "Use read_worker_result(job_id) to get full details. "
        "This prevents context overflow when scanning 50+ workers.",
    ),
    StructuredTool.from_function(
        coroutine=read_worker_result_async,
        name="read_worker_result",
        description="Read the final result from a completed worker job. "
        "Provide the job ID (integer) to get the natural language result text.",
    ),
    StructuredTool.from_function(
        coroutine=read_worker_file_async,
        name="read_worker_file",
        description="Read a specific file from a worker job's artifacts. "
        "Provide the job ID (integer) and file path to drill into worker details like "
        "tool outputs (tool_calls/*.txt), conversation history (thread.jsonl), or metadata (metadata.json).",
    ),
    StructuredTool.from_function(
        coroutine=grep_workers_async,
        name="grep_workers",
        description="Search across completed worker job artifacts for a text pattern. "
        "Performs case-insensitive search and returns matches with job IDs and context. "
        "Useful for finding jobs that encountered specific errors or outputs.",
    ),
    StructuredTool.from_function(
        coroutine=get_worker_metadata_async,
        name="get_worker_metadata",
        description="Get detailed metadata about a worker job execution including "
        "task, status, timestamps, duration, and configuration. "
        "Provide the job ID (integer) to inspect job details.",
    ),
]
