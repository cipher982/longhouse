"""Runner execution tools.

Tools for executing commands on user-owned runners. This implements the
Execution Connectors v1 architecture where runners are user-managed compute
that the backend can delegate work to without needing SSH keys.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any
from typing import Dict
from typing import List

from langchain_core.tools import StructuredTool

from zerg.context import get_commis_context
from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.services.command_validator import CommandValidator
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)


def _run_coro_sync(coro: Any) -> Dict[str, Any]:
    """Run an async coroutine from a sync context.

    runner_exec is registered as a synchronous LangChain tool. In most cases it
    runs without an active event loop. If called from within an event loop,
    calling asyncio.run() would raise; instead we execute the coroutine in a
    dedicated thread with its own event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_commis=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


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

    This tool enables commis fiches to execute commands on user-managed compute
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
    # Get commis context for owner_id
    ctx = get_commis_context()
    if not ctx or ctx.owner_id is None:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "runner_exec requires commis context with owner_id",
        )

    owner_id = ctx.owner_id
    commis_id = ctx.commis_id
    course_id = ctx.course_id

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
            (
                f"Runner '{target}' not found (no Runner daemon is connected with that name). "
                "This refers to the Runner connector, not the server itself."
            ),
        )

    runner_id, runner_name = resolved

    # Check runner status and get runner capabilities
    db = next(get_db())
    try:
        runner = runner_crud.get_runner(db, runner_id)
        if not runner:
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                (
                    f"Runner '{target}' not found (no Runner daemon is connected with that name). "
                    "This refers to the Runner connector, not the server itself."
                ),
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

        result = _run_coro_sync(
            dispatcher.dispatch_job(
                db=db,
                owner_id=owner_id,
                runner_id=runner_id,
                command=command,
                timeout_secs=timeout_secs,
                commis_id=commis_id,
                course_id=course_id,
            )
        )

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
        return tool_success(
            {
                "target": runner_name,
                "command": command,
                "exit_code": data.get("exit_code", -1),
                "stdout": data.get("stdout", ""),
                "stderr": data.get("stderr", ""),
                "duration_ms": data.get("duration_ms", 0),
            }
        )

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
