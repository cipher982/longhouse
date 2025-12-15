"""Runner execution tools.

Tools for executing commands on user-owned runners. This implements the
Execution Connectors v1 architecture where runners are user-managed compute
that the backend can delegate work to without needing SSH keys.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from typing import Dict
from typing import List

from langchain_core.tools import StructuredTool

from zerg.context import get_worker_context
from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.services.command_validator import CommandValidator
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)


def _resolve_target(owner_id: int, target: str) -> tuple[int, str] | None:
    """Resolve target string to runner ID and name.

    Supports both explicit runner:ID format and name-based lookup.

    Args:
        owner_id: ID of the user owning the runner
        target: Runner identifier - either "runner:123" or "laptop"

    Returns:
        Tuple of (runner_id, runner_name) or None if not found
    """
    db = next(get_db())

    try:
        # Check for explicit ID format
        if target.startswith("runner:"):
            try:
                runner_id = int(target.split(":")[1])
                runner = runner_crud.get_runner(db, runner_id)
                if runner and runner.owner_id == owner_id:
                    return (runner.id, runner.name)
                return None
            except (ValueError, IndexError):
                return None

        # Name-based lookup
        runner = runner_crud.get_runner_by_name(db, owner_id, target)
        if runner:
            return (runner.id, runner.name)

        return None
    finally:
        db.close()


def runner_exec(
    target: str,
    command: str,
    timeout_secs: int = 30,
) -> Dict[str, Any]:
    """Execute a command on a user-owned runner.

    This tool enables worker agents to execute commands on user-managed compute
    infrastructure (laptops, servers, containers) without the backend needing
    SSH keys or direct access.

    Target resolution:
    - "laptop", "home-server", etc: Resolved by name (user-specific)
    - "runner:123": Explicit runner ID

    Security:
    - Only the runner's owner can execute commands on it
    - Runners authenticate with secret tokens
    - Commands are executed in the runner's configured environment
    - Output is truncated at 50KB to prevent unbounded growth

    Args:
        target: Runner name (e.g., "laptop") or ID (e.g., "runner:123")
        command: Shell command to execute
        timeout_secs: Maximum seconds to wait (default: 30)

    Returns:
        Success envelope with:
        - target: Runner name
        - command: Command that was executed
        - exit_code: Command exit code (0 = success)
        - stdout: Standard output
        - stderr: Standard error
        - duration_ms: Execution time in milliseconds

        Or error envelope for:
        - Runner not found
        - Runner offline
        - Runner revoked
        - Runner busy (concurrency limit)
        - Timeout waiting for response
        - Execution error

    Example:
        >>> runner_exec("laptop", "df -h")
        {
            "ok": True,
            "data": {
                "target": "laptop",
                "command": "df -h",
                "exit_code": 0,
                "stdout": "Filesystem      Size  Used...",
                "stderr": "",
                "duration_ms": 234
            }
        }

    Note: Non-zero exit codes are NOT errors - they indicate the command ran
    but returned a failure code. Only connection/timeout failures are errors.
    """
    # Get worker context for owner_id
    ctx = get_worker_context()
    if not ctx or ctx.owner_id is None:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "runner_exec requires worker context with owner_id",
        )

    owner_id = ctx.owner_id
    worker_id = ctx.worker_id
    run_id = ctx.run_id

    # Validate parameters
    if not target:
        return tool_error(ErrorType.VALIDATION_ERROR, "target parameter is required")

    if not command:
        return tool_error(ErrorType.VALIDATION_ERROR, "command parameter is required")

    if timeout_secs <= 0:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "timeout_secs must be positive",
        )

    # Resolve target to runner
    resolved = _resolve_target(owner_id, target)
    if not resolved:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            f"Runner '{target}' not found",
        )

    runner_id, runner_name = resolved

    # Check runner status and get runner capabilities
    db = next(get_db())
    try:
        runner = runner_crud.get_runner(db, runner_id)
        if not runner:
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                f"Runner '{target}' not found",
            )

        if runner.status == "revoked":
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                f"Runner '{runner_name}' has been revoked",
            )

        if runner.status == "offline":
            return tool_error(
                ErrorType.EXECUTION_ERROR,
                f"Runner '{runner_name}' is offline",
            )

        # Validate command against runner capabilities
        validator = CommandValidator()
        allowed, reason = validator.validate(command, runner.capabilities)
        if not allowed:
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                f"Command not allowed: {reason}",
            )

        # Dispatch job and wait for completion
        dispatcher = get_runner_job_dispatcher()

        # Run async dispatcher - try to get running loop or create new one
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, use asyncio.run
            result = asyncio.run(
                dispatcher.dispatch_job(
                    db=db,
                    owner_id=owner_id,
                    runner_id=runner_id,
                    command=command,
                    timeout_secs=timeout_secs,
                    worker_id=worker_id,
                    run_id=run_id,
                )
            )
        else:
            # Running in async context, await directly
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = loop.run_in_executor(
                    pool,
                    lambda: asyncio.run(
                        dispatcher.dispatch_job(
                            db=db,
                            owner_id=owner_id,
                            runner_id=runner_id,
                            command=command,
                            timeout_secs=timeout_secs,
                            worker_id=worker_id,
                            run_id=run_id,
                        )
                    ),
                )
                result = loop.run_until_complete(result)

        # Check if result is an error
        if not result.get("ok"):
            error = result.get("error", {})
            error_type = error.get("type", "execution_error")
            error_message = error.get("message", "Unknown error")
            return tool_error(
                ErrorType.EXECUTION_ERROR if error_type == "execution_error" else ErrorType.VALIDATION_ERROR,
                error_message,
            )

        # Transform result to match ssh_exec envelope
        data = result.get("data", {})
        return tool_success({
            "target": runner_name,
            "command": command,
            "exit_code": data.get("exit_code", -1),
            "stdout": data.get("stdout", ""),
            "stderr": data.get("stderr", ""),
            "duration_ms": data.get("duration_ms", 0),
        })

    except Exception as e:
        logger.exception(f"Error executing command on runner {runner_name}")
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            f"Unexpected error: {str(e)}",
        )
    finally:
        db.close()


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=runner_exec,
        name="runner_exec",
        description=(
            "Execute a shell command on a user-owned runner (laptop, server, container). "
            "Runners are user-managed compute infrastructure. Use target name (e.g., 'laptop') "
            "or explicit ID (e.g., 'runner:123'). Returns exit code, stdout, stderr, and duration. "
            "Non-zero exit codes are not errors - they indicate the command ran but returned a failure code."
        ),
    ),
]
