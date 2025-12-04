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
from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)


def spawn_worker(task: str, model: str = "gpt-4o-mini") -> str:
    """Spawn a worker agent to execute a task.

    The worker runs independently, persists all outputs to disk, and returns
    a natural language result. Use this when you need to delegate work that
    might involve multiple tool calls or generate verbose output.

    Args:
        task: Natural language description of what the worker should do
        model: LLM model for the worker (default: gpt-4o-mini)

    Returns:
        A summary containing the worker_id and the worker's result

    Example:
        spawn_worker("Check disk usage on cube server via SSH")
        spawn_worker("Research the top 5 robot vacuums under $500", model="gpt-4o")
    """
    # Lazy import to avoid circular dependency
    from zerg.services.worker_runner import WorkerRunner
    from zerg.utils.async_utils import run_async_safely

    # Get database session from credential resolver context
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot spawn worker - no credential context available"

    db = resolver.db
    owner_id = resolver.owner_id

    # Create runner and store
    artifact_store = WorkerArtifactStore()
    runner = WorkerRunner(artifact_store=artifact_store)

    # Run worker (safely handling async context)
    try:
        # Use run_async_safely to handle both sync and async contexts
        result = run_async_safely(
            runner.run_worker(
                db=db,
                task=task,
                agent=None,  # Create temporary agent
                agent_config={
                    "model": model,
                    "owner_id": owner_id,
                },
            )
        )

        if result.status == "success":
            return (
                f"Worker {result.worker_id} completed successfully.\n\n"
                f"Task: {task}\n"
                f"Duration: {result.duration_ms}ms\n\n"
                f"Result:\n{result.result}"
            )
        else:
            return (
                f"Worker {result.worker_id} failed.\n\n"
                f"Task: {task}\n"
                f"Error: {result.error}"
            )

    except Exception as e:
        logger.exception(f"Failed to spawn worker for task: {task}")
        return f"Error spawning worker: {e}"


def list_workers(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """List recent worker executions.

    Args:
        limit: Maximum number of workers to return (default: 20)
        status: Filter by status ("success", "failed", or None for all)
        since_hours: Only show workers from the last N hours

    Returns:
        Formatted list of workers with their IDs, tasks, status, and timestamps
    """
    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot list workers - no credential context available"

    artifact_store = WorkerArtifactStore()

    # Calculate since timestamp if specified
    since = None
    if since_hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    try:
        workers = artifact_store.list_workers(
            limit=limit,
            status=status,
            since=since,
            owner_id=resolver.owner_id,
        )

        if not workers:
            return "No workers found matching criteria."

        # Format output
        lines = [f"Found {len(workers)} worker(s):\n"]
        for worker in workers:
            worker_id = worker.get("worker_id", "unknown")
            task = worker.get("task", "")
            worker_status = worker.get("status", "unknown")
            created_at = worker.get("created_at", "")
            duration_ms = worker.get("duration_ms")

            # Truncate task for display
            task_display = task[:60] + "..." if len(task) > 60 else task

            duration_str = f"{duration_ms}ms" if duration_ms else "N/A"

            lines.append(
                f"\n[{worker_status.upper()}] {worker_id}\n"
                f"  Task: {task_display}\n"
                f"  Created: {created_at}\n"
                f"  Duration: {duration_str}"
            )

        return "\n".join(lines)

    except Exception as e:
        logger.exception("Failed to list workers")
        return f"Error listing workers: {e}"


def read_worker_result(worker_id: str) -> str:
    """Read the final result from a completed worker.

    Args:
        worker_id: The worker ID (e.g., "2024-12-03T14-32-00_disk-check")

    Returns:
        The worker's natural language result
    """
    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot read worker result - no credential context available"

    artifact_store = WorkerArtifactStore()

    try:
        # Verify access by checking metadata first
        artifact_store.get_worker_metadata(worker_id, owner_id=resolver.owner_id)
        
        result = artifact_store.get_worker_result(worker_id)
        return f"Result from worker {worker_id}:\n\n{result}"
    except PermissionError:
        return f"Error: Access denied to worker {worker_id}"
    except FileNotFoundError:
        return f"Error: Worker {worker_id} not found or has no result yet"
    except Exception as e:
        logger.exception(f"Failed to read worker result: {worker_id}")
        return f"Error reading worker result: {e}"


def read_worker_file(worker_id: str, file_path: str) -> str:
    """Read a specific file from a worker's artifacts.

    Use this to drill into worker details like tool outputs or full conversation.

    Args:
        worker_id: The worker ID
        file_path: Relative path within worker directory (e.g., "tool_calls/001_ssh_exec.txt")

    Returns:
        Contents of the file

    Common paths:
        - "result.txt" - Final result
        - "metadata.json" - Worker metadata (status, timestamps, config)
        - "thread.jsonl" - Full conversation history
        - "tool_calls/*.txt" - Individual tool outputs
    """
    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot read worker file - no credential context available"

    artifact_store = WorkerArtifactStore()

    try:
        # Verify access by checking metadata first
        artifact_store.get_worker_metadata(worker_id, owner_id=resolver.owner_id)
        
        content = artifact_store.read_worker_file(worker_id, file_path)
        return f"Contents of {file_path} from worker {worker_id}:\n\n{content}"
    except PermissionError:
        return f"Error: Access denied to worker {worker_id}"
    except FileNotFoundError:
        return f"Error: File {file_path} not found in worker {worker_id}"
    except ValueError as e:
        return f"Error: Invalid file path - {e}"
    except Exception as e:
        logger.exception(f"Failed to read worker file: {worker_id}/{file_path}")
        return f"Error reading worker file: {e}"


def grep_workers(pattern: str, since_hours: int = 24) -> str:
    """Search across worker artifacts for a pattern.

    Args:
        pattern: Text pattern to search for (case-insensitive)
        since_hours: Only search workers from the last N hours (default: 24)

    Returns:
        Matches with worker IDs and context
    """
    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot grep workers - no credential context available"

    artifact_store = WorkerArtifactStore()

    try:
        # Use case-insensitive regex
        import re

        case_insensitive_pattern = f"(?i){re.escape(pattern)}"

        # Search across all text files
        matches = artifact_store.search_workers(
            pattern=case_insensitive_pattern,
            file_glob="**/*.txt",
        )

        # Filter by time AND owner
        filtered_matches = []
        
        # Calculate cutoff time
        cutoff_iso = None
        if since_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            cutoff_iso = cutoff.isoformat()

        owner_id = resolver.owner_id
        
        for m in matches:
            metadata = m.get("metadata", {})
            
            # Filter by time if specified
            if cutoff_iso and metadata.get("created_at", "") < cutoff_iso:
                continue
                
            # Filter by owner
            worker_owner = metadata.get("config", {}).get("owner_id")
            if worker_owner is not None and worker_owner != owner_id:
                continue
                
            filtered_matches.append(m)
        
        matches = filtered_matches

        if not matches:
            return f"No matches found for pattern '{pattern}' in last {since_hours} hours"

        # Format results
        lines = [f"Found {len(matches)} match(es) for '{pattern}':\n"]
        for match in matches[:50]:  # Limit to 50 matches
            worker_id = match.get("worker_id", "unknown")
            file_name = match.get("file", "unknown")
            line_num = match.get("line", 0)
            content = match.get("content", "")

            lines.append(
                f"\n{worker_id}/{file_name}:{line_num}\n" f"  {content[:200]}"
            )

        if len(matches) > 50:
            lines.append(f"\n... and {len(matches) - 50} more matches (truncated)")

        return "\n".join(lines)

    except Exception as e:
        logger.exception(f"Failed to grep workers: {pattern}")
        return f"Error searching workers: {e}"


def get_worker_metadata(worker_id: str) -> str:
    """Get detailed metadata about a worker execution.

    Args:
        worker_id: The worker ID

    Returns:
        Formatted metadata including task, status, timestamps, duration, config
    """
    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return "Error: Cannot get worker metadata - no credential context available"

    artifact_store = WorkerArtifactStore()

    try:
        metadata = artifact_store.get_worker_metadata(worker_id, owner_id=resolver.owner_id)

        # Format nicely
        lines = [
            f"Metadata for worker {worker_id}:\n",
            f"Status: {metadata.get('status', 'unknown')}",
            f"Task: {metadata.get('task', '')}",
            f"\nTimestamps:",
            f"  Created: {metadata.get('created_at', 'N/A')}",
            f"  Started: {metadata.get('started_at', 'N/A')}",
            f"  Finished: {metadata.get('finished_at', 'N/A')}",
            f"  Duration: {metadata.get('duration_ms', 'N/A')}ms",
        ]

        # Add config if present
        if metadata.get("config"):
            lines.append(f"\nConfiguration:")
            for key, value in metadata["config"].items():
                lines.append(f"  {key}: {value}")

        # Add error if present
        if metadata.get("error"):
            lines.append(f"\nError: {metadata['error']}")

        return "\n".join(lines)

    except PermissionError:
        return f"Error: Access denied to worker {worker_id}"
    except FileNotFoundError:
        return f"Error: Worker {worker_id} not found"
    except Exception as e:
        logger.exception(f"Failed to get worker metadata: {worker_id}")
        return f"Error getting worker metadata: {e}"


# Export tools list for registration
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=spawn_worker,
        name="spawn_worker",
        description="Spawn a worker agent to execute a task independently. "
        "The worker persists all outputs and returns a natural language result. "
        "Use for delegation, parallel work, or tasks that might generate verbose output.",
    ),
    StructuredTool.from_function(
        func=list_workers,
        name="list_workers",
        description="List recent worker executions with optional filters for status and time range. "
        "Returns worker IDs, tasks, status, and timestamps.",
    ),
    StructuredTool.from_function(
        func=read_worker_result,
        name="read_worker_result",
        description="Read the final result from a completed worker. "
        "Returns the natural language result text.",
    ),
    StructuredTool.from_function(
        func=read_worker_file,
        name="read_worker_file",
        description="Read a specific file from a worker's artifacts. "
        "Use to drill into worker details like tool outputs (tool_calls/*.txt), "
        "conversation history (thread.jsonl), or metadata (metadata.json).",
    ),
    StructuredTool.from_function(
        func=grep_workers,
        name="grep_workers",
        description="Search across worker artifacts for a text pattern. "
        "Performs case-insensitive search and returns matches with context. "
        "Useful for finding workers that encountered specific errors or outputs.",
    ),
    StructuredTool.from_function(
        func=get_worker_metadata,
        name="get_worker_metadata",
        description="Get detailed metadata about a worker execution including "
        "task, status, timestamps, duration, and configuration. "
        "Use to inspect worker details without reading full artifacts.",
    ),
]
