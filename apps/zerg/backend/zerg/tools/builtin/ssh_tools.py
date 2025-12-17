"""SSH execution tools via runners.

This module provides tools for executing commands on SSH targets that are
configured as account-level connectors. Commands are dispatched to a runner
which has access to the SSH targets via its local SSH config or keys.

Architecture:
- SSH targets are configured as AccountConnectorCredential with type="ssh"
- When runner_ssh_exec is called, it resolves the target from the credential
- The actual SSH execution happens on the runner (uses runner's SSH access)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import shlex
from typing import Any
from typing import Dict
from typing import List

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.context import get_worker_context
from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.models.models import AccountConnectorCredential
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success
from zerg.utils.crypto import decrypt

logger = logging.getLogger(__name__)


def _run_coro_sync(coro: Any) -> Dict[str, Any]:
    """Run an async coroutine from a sync context."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def _resolve_ssh_target(owner_id: int, target: str, db: Any) -> dict[str, Any] | None:
    """Resolve SSH target name to connection details from AccountConnectorCredential.

    Args:
        owner_id: User ID
        target: SSH target name (e.g., "prod-web-1")
        db: Database session

    Returns:
        Dict with SSH connection details or None if not found
    """
    import json

    # Query all SSH credentials for this owner
    ssh_creds = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == owner_id,
            AccountConnectorCredential.connector_type == ConnectorType.SSH.value,
        )
        .all()
    )

    # Find credential with matching name
    for cred in ssh_creds:
        try:
            decrypted = decrypt(cred.encrypted_value)
            cred_data = json.loads(decrypted)
            if cred_data.get("name") == target:
                return cred_data
        except Exception as e:
            logger.warning(f"Failed to decrypt SSH credential {cred.id}: {e}")
            continue

    return None


def _find_online_runner(owner_id: int, db: Any) -> tuple[int, str] | None:
    """Find an online runner for the user.

    Args:
        owner_id: User ID
        db: Database session

    Returns:
        Tuple of (runner_id, runner_name) or None if no online runner
    """
    runners = runner_crud.get_runners(db=db, owner_id=owner_id, limit=100)
    for runner in runners:
        if runner.status == "online":
            return (runner.id, runner.name)
    return None


def runner_ssh_exec(
    target: str,
    command: str,
    runner: str | None = None,
    timeout_secs: int = 30,
) -> Dict[str, Any]:
    """Execute a command on an SSH target via a runner.

    SSH targets are configured in your account settings. The command is
    dispatched to a runner which executes it on the target using SSH.

    Args:
        target: SSH target name (e.g., "prod-web-1") as configured in account settings
        command: Shell command to execute on the SSH target
        runner: Optional runner name/ID to use. If not specified, uses first online runner.
        timeout_secs: Maximum seconds to wait (default: 30)

    Returns:
        Success envelope with:
        - target: SSH target name
        - command: Command that was executed
        - exit_code: Command exit code (0 = success)
        - stdout: Standard output
        - stderr: Standard error
        - duration_ms: Execution time in milliseconds
        - runner: Name of runner that executed the command

        Or error envelope for:
        - SSH target not found (not configured)
        - No online runner available
        - SSH connection failed
        - Timeout
        - Command execution error

    Example:
        >>> runner_ssh_exec("prod-web-1", "df -h")
        {
            "ok": True,
            "data": {
                "target": "prod-web-1",
                "command": "df -h",
                "exit_code": 0,
                "stdout": "Filesystem      Size  Used...",
                "stderr": "",
                "duration_ms": 1234,
                "runner": "my-laptop"
            }
        }

    Note: The runner must have SSH access to the target (via its ~/.ssh/config,
    SSH agent, or key files). SSH keys are NOT stored in Swarmlet.
    """
    # Get worker context for owner_id
    ctx = get_worker_context()
    if not ctx or ctx.owner_id is None:
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "runner_ssh_exec requires worker context with owner_id",
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

    db = next(get_db())
    try:
        # Resolve SSH target from account credentials
        ssh_config = _resolve_ssh_target(owner_id, target, db)
        if not ssh_config:
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                f"SSH target '{target}' not found. Configure it in Settings → Integrations → SSH.",
            )

        # Find a runner to use
        if runner:
            # Specific runner requested
            if runner.startswith("runner:"):
                runner_id = int(runner.split(":")[1])
                runner_record = runner_crud.get_runner(db, runner_id)
            else:
                runner_record = runner_crud.get_runner_by_name(db, owner_id, runner)

            if not runner_record or runner_record.owner_id != owner_id:
                return tool_error(
                    ErrorType.VALIDATION_ERROR,
                    f"Runner '{runner}' not found",
                )
            if runner_record.status != "online":
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    f"Runner '{runner_record.name}' is {runner_record.status}",
                )
            runner_id = runner_record.id
            runner_name = runner_record.name
        else:
            # Use first online runner
            result = _find_online_runner(owner_id, db)
            if not result:
                return tool_error(
                    ErrorType.EXECUTION_ERROR,
                    "No online runners available. Connect a runner first.",
                )
            runner_id, runner_name = result

        # Build SSH command for the runner
        # The runner will execute this via its local SSH
        ssh_host = ssh_config.get("ssh_config_name") or ssh_config.get("host")
        ssh_user = ssh_config.get("user")
        ssh_port = ssh_config.get("port", 22)

        if not ssh_host or not str(ssh_host).strip():
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                f"SSH target '{target}' is missing host configuration. Update it in Settings → Integrations → SSH.",
            )

        ssh_host = str(ssh_host).strip()

        # Build the SSH command
        ssh_parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
        if ssh_port and ssh_port != 22:
            ssh_parts.extend(["-p", str(ssh_port)])
        if ssh_user:
            ssh_parts.append(f"{ssh_user}@{ssh_host}")
        else:
            ssh_parts.append(ssh_host)

        # Add the command
        # Keep this as a single argv element; shlex.join will quote it safely.
        ssh_parts.append(command)

        # Join into full command
        ssh_command = shlex.join([str(p) for p in ssh_parts])

        # Dispatch to runner
        dispatcher = get_runner_job_dispatcher()
        result = _run_coro_sync(
            dispatcher.dispatch_job(
                db=db,
                owner_id=owner_id,
                runner_id=runner_id,
                command=ssh_command,
                timeout_secs=timeout_secs,
                worker_id=worker_id,
                run_id=run_id,
            )
        )

        # Check if result is an error
        if not result.get("ok"):
            error = result.get("error", {})
            error_message = error.get("message", "Unknown error")
            return tool_error(
                ErrorType.EXECUTION_ERROR,
                f"SSH execution failed: {error_message}",
            )

        # Transform result
        data = result.get("data", {})
        return tool_success({
            "target": target,
            "command": command,
            "exit_code": data.get("exit_code", -1),
            "stdout": data.get("stdout", ""),
            "stderr": data.get("stderr", ""),
            "duration_ms": data.get("duration_ms", 0),
            "runner": runner_name,
        })

    except Exception as e:
        logger.exception(f"Error executing SSH command on target {target}")
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            f"Unexpected error: {str(e)}",
        )
    finally:
        db.close()


def ssh_target_list() -> Dict[str, Any]:
    """List configured SSH targets for the current user.

    Returns all SSH targets configured in account settings.
    Use this to see what SSH targets are available for runner_ssh_exec.

    Returns:
        Success envelope with:
        - targets: List of SSH target configurations (name, host, user, etc.)

    Example:
        >>> ssh_target_list()
        {
            "ok": True,
            "data": {
                "targets": [
                    {"name": "prod-web-1", "host": "192.168.1.10", "user": "deploy"},
                    {"name": "staging", "ssh_config_name": "staging-server"}
                ]
            }
        }
    """
    import json

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            "No credential context available",
        )

    db = resolver.db
    owner_id = resolver.owner_id

    # Query all SSH credentials for this owner
    ssh_creds = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == owner_id,
            AccountConnectorCredential.connector_type == ConnectorType.SSH.value,
        )
        .all()
    )

    targets = []
    for cred in ssh_creds:
        try:
            decrypted = decrypt(cred.encrypted_value)
            cred_data = json.loads(decrypted)
            # Include only non-sensitive info
            target_info = {
                "name": cred_data.get("name"),
                "host": cred_data.get("host"),
            }
            if cred_data.get("user"):
                target_info["user"] = cred_data["user"]
            if cred_data.get("port"):
                target_info["port"] = cred_data["port"]
            if cred_data.get("ssh_config_name"):
                target_info["ssh_config_name"] = cred_data["ssh_config_name"]
            targets.append(target_info)
        except Exception as e:
            logger.warning(f"Failed to decrypt SSH credential {cred.id}: {e}")
            continue

    return tool_success({"targets": targets})


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=runner_ssh_exec,
        name="runner_ssh_exec",
        description=(
            "Execute a shell command on an SSH target via a runner. "
            "SSH targets are configured in Settings → Integrations. "
            "The runner handles the SSH connection using its local SSH config/keys. "
            "Use ssh_target_list to see available targets."
        ),
    ),
    StructuredTool.from_function(
        func=ssh_target_list,
        name="ssh_target_list",
        description="List configured SSH targets available for runner_ssh_exec.",
    ),
]
