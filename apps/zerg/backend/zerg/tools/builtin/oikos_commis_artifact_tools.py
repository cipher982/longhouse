"""Commis artifact access tools extracted from oikos_tools."""

import logging

from zerg.connectors.context import get_credential_resolver
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.tool_output_store import ToolOutputStore
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error

logger = logging.getLogger(__name__)


def format_duration(duration_ms: int) -> str:
    """Format duration for human readability."""
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    elif duration_ms < 60000:
        seconds = duration_ms / 1000
        return f"{seconds:.1f}s"
    else:
        minutes = duration_ms // 60000
        remaining_seconds = (duration_ms % 60000) // 1000
        return f"{minutes}m {remaining_seconds}s"


async def read_commis_result_async(job_id: str) -> str:
    """Read the final result from a completed commis job."""
    from zerg.crud import crud

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot read commis result - no credential context available",
        )

    db = resolver.db

    try:
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
            return tool_error(ErrorType.NOT_FOUND, f"Commis job {job_id} not found")
        if not job.commis_id:
            return tool_error(ErrorType.INVALID_STATE, f"Commis job {job_id} has not started execution yet")
        if job.status not in ["success", "failed"]:
            return tool_error(ErrorType.INVALID_STATE, f"Commis job {job_id} is not complete (status: {job.status})")

        artifact_store = CommisArtifactStore()
        result = artifact_store.get_commis_result(job.commis_id)
        metadata = artifact_store.get_commis_metadata(job.commis_id, owner_id=resolver.owner_id)

        duration_ms = metadata.get("duration_ms")
        duration_info = f"\n\nExecution time: {format_duration(duration_ms)}" if duration_ms is not None else ""

        return f"Result from commis job {job_id} (commis {job.commis_id}):{duration_info}\n\n{result}"

    except ValueError:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid job ID format: {job_id}")
    except PermissionError:
        return tool_error(ErrorType.PERMISSION_DENIED, f"Access denied to commis job {job_id}")
    except FileNotFoundError:
        return tool_error(ErrorType.NOT_FOUND, f"Commis job {job_id} not found or has no result yet")
    except Exception as e:
        logger.exception(f"Failed to read commis result: {job_id}")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error reading commis result: {e}")


def read_commis_result(job_id: str) -> str:
    """Sync wrapper for read_commis_result_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(read_commis_result_async(job_id))


async def get_commis_evidence_async(job_id: str, budget_bytes: int = 32000) -> str:
    """Compile evidence for a commis job within a byte budget."""
    from zerg.crud import crud
    from zerg.services.evidence_compiler import EvidenceCompiler

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot fetch evidence - no credential context available",
        )

    db = resolver.db
    safe_budget = max(1024, min(int(budget_bytes or 0), 200_000))

    try:
        job_id_int = int(job_id)
    except ValueError:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid job ID format: {job_id}")

    try:
        job = (
            db.query(crud.CommisJob)
            .filter(
                crud.CommisJob.id == job_id_int,
                crud.CommisJob.owner_id == resolver.owner_id,
            )
            .first()
        )
        if not job:
            return tool_error(ErrorType.NOT_FOUND, f"Commis job {job_id} not found")
        if not job.commis_id:
            return tool_error(ErrorType.INVALID_STATE, f"Commis job {job_id} has not started execution yet")

        compiler = EvidenceCompiler(db=db)
        evidence = compiler.compile_for_job(
            job_id=job.id,
            commis_id=job.commis_id,
            owner_id=resolver.owner_id,
            budget_bytes=safe_budget,
        )

        return f"Evidence for commis job {job_id} (commis {job.commis_id}, budget={safe_budget}B):\n\n{evidence}"

    except PermissionError:
        return tool_error(ErrorType.PERMISSION_DENIED, f"Access denied to commis job {job_id}")
    except Exception as e:
        logger.exception(f"Failed to compile evidence for commis job: {job_id}")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error compiling evidence for commis job {job_id}: {e}")


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
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot fetch tool output - no credential context available",
        )

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
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid artifact_id: {artifact_id}")
    except FileNotFoundError:
        return tool_error(ErrorType.NOT_FOUND, f"Tool output {artifact_id} not found")
    except Exception as e:
        logger.exception("Failed to read tool output: %s", artifact_id)
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error reading tool output {artifact_id}: {e}")


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
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot read commis file - no credential context available",
        )

    db = resolver.db

    try:
        # Parse job ID
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
            return tool_error(ErrorType.NOT_FOUND, f"Commis job {job_id} not found")

        if not job.commis_id:
            return tool_error(ErrorType.INVALID_STATE, f"Commis job {job_id} has not started execution yet")

        # Read file from artifacts
        artifact_store = CommisArtifactStore()
        # Verify access by checking metadata first
        artifact_store.get_commis_metadata(job.commis_id, owner_id=resolver.owner_id)

        content = artifact_store.read_commis_file(job.commis_id, file_path)
        return f"Contents of {file_path} from commis job {job_id} (commis {job.commis_id}):\n\n{content}"

    except ValueError:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid job ID format: {job_id}")
    except PermissionError:
        return tool_error(ErrorType.PERMISSION_DENIED, f"Access denied to commis job {job_id}")
    except FileNotFoundError:
        return tool_error(ErrorType.NOT_FOUND, f"File {file_path} not found in commis job {job_id}")
    except Exception as e:
        logger.exception(f"Failed to read commis file: {job_id}/{file_path}")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error reading commis file: {e}")


async def peek_commis_output_async(job_id: str, max_bytes: int = 4000) -> str:
    """Peek live output for a running commis job (tail buffer).

    Args:
        job_id: Commis job ID
        max_bytes: Max bytes to return from the tail (0 = full buffer)

    Returns:
        Live output tail or a helpful status message.
    """
    from zerg.crud import crud

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot peek commis output - no credential context available",
        )

    db = resolver.db

    try:
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
            return tool_error(ErrorType.NOT_FOUND, f"Commis job {job_id} not found")

        if not job.commis_id:
            return f"Commis job {job_id} has not started execution yet"

        execution_mode = (job.config or {}).get("execution_mode")
        if execution_mode == "workspace":
            return (
                f"Commis job {job_id} is a workspace commis and does not stream live output. "
                f"Use check_commis_status({job_id}) or read_commis_result({job_id}) after completion."
            )

        from zerg.models.models import RunnerJob
        from zerg.services.commis_output_buffer import get_commis_output_buffer

        output_buffer = get_commis_output_buffer()
        live = output_buffer.get_tail(job.commis_id, max_bytes=max_bytes)
        if live:
            return f"Live commis output (tail):\n\n{live}"

        # Fallback: last known runner job output (not live if buffer empty)
        runner_job = (
            db.query(RunnerJob)
            .filter(
                RunnerJob.commis_id == job.commis_id,
                RunnerJob.owner_id == resolver.owner_id,
            )
            .order_by(RunnerJob.created_at.desc())
            .first()
        )
        if runner_job and (runner_job.stdout_trunc or runner_job.stderr_trunc):
            combined = runner_job.stdout_trunc or ""
            if runner_job.stderr_trunc:
                combined = f"{combined}\n[stderr]\n{runner_job.stderr_trunc}" if combined else f"[stderr]\n{runner_job.stderr_trunc}"
            if max_bytes and max_bytes > 0 and len(combined) > max_bytes:
                combined = combined[-max_bytes:]
            return f"Recent runner output (tail):\n\n{combined}"

        if job.status in ["success", "failed", "cancelled"]:
            return (
                f"Commis job {job_id} finished with status {job.status.upper()}. "
                f"Use read_commis_result({job_id}) for the final summary."
            )

        return "No live output yet. Try again soon."
    except ValueError:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid job ID format: {job_id}")
    except Exception as e:
        logger.exception(f"Failed to peek commis output: {job_id}")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error peeking commis output: {e}")


def read_commis_file(job_id: str, file_path: str) -> str:
    """Sync wrapper for read_commis_file_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(read_commis_file_async(job_id, file_path))


def peek_commis_output(job_id: str, max_bytes: int = 4000) -> str:
    """Sync wrapper for peek_commis_output_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(peek_commis_output_async(job_id, max_bytes))
