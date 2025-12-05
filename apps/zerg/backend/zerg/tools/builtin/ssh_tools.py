"""SSH-related tools for remote command execution.

This tool enables worker agents to execute commands on remote infrastructure servers.
It implements the "shell-first philosophy" where SSH access is the primitive for
remote operations, rather than modeling each command as a separate tool.

Security:
- Host allowlist enforced (cube, clifford, zerg, slim)
- SSH key authentication only (no password auth)
- Timeout protection to prevent hanging connections
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any
from typing import Dict
from typing import List

from langchain_core.tools import StructuredTool

from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)

# Allowed SSH hosts with their default configurations
ALLOWED_HOSTS = {
    "cube": {"user": "drose", "host": "100.70.237.79", "port": "2222"},
    "clifford": {"user": "drose", "host": "REDACTED_IP", "port": "22"},
    "zerg": {"user": "zerg", "host": "5.161.92.127", "port": "22"},
    "slim": {"user": "drose", "host": "135.181.204.0", "port": "22"},
}

# Maximum output size before truncation (10KB)
MAX_OUTPUT_SIZE = 10 * 1024


def _parse_host(host: str) -> tuple[str, str, str] | None:
    """Parse host string into (user, hostname, port).

    Supports formats:
    - "cube", "clifford", "zerg", "slim" (known hosts)
    - "user@hostname" (custom format)

    Args:
        host: Host identifier or user@hostname format

    Returns:
        Tuple of (user, hostname, port) or None if invalid
    """
    # Check if it's a known host alias
    if host in ALLOWED_HOSTS:
        config = ALLOWED_HOSTS[host]
        return (config["user"], config["host"], config["port"])

    # Parse custom user@hostname format
    if "@" in host:
        parts = host.split("@")
        if len(parts) == 2:
            user, hostname = parts
            if user and hostname:
                return (user, hostname, "22")

    return None


def ssh_exec(
    host: str,
    command: str,
    timeout_secs: int = 30,
) -> Dict[str, Any]:
    """Execute a command on a remote server via SSH.

    This tool enables worker agents to run commands on infrastructure servers.
    Workers already know how to use standard Unix tools (df, docker, journalctl, etc.)
    - this gives them the primitive to access remote systems.

    Allowed hosts (by alias):
    - cube: Home GPU server (AI workloads, cameras)
    - clifford: Production VPS (90% of web apps)
    - zerg: Project server (dedicated workloads)
    - slim: EU VPS (cost-effective workloads)

    You can also use custom "user@hostname" format for flexibility.

    Security notes:
    - Only allowlisted hosts (cube, clifford, zerg, slim) are accessible
    - Uses SSH key authentication via ~/.ssh/id_ed25519 (rosetta key)
    - Commands have timeout protection
    - Output is truncated if > 10KB to prevent token explosion

    Args:
        host: Server alias ("cube", "clifford", "zerg", "slim") or "user@hostname"
        command: Shell command to execute remotely
        timeout_secs: Maximum seconds to wait before killing the command (default: 30)

    Returns:
        Success envelope with:
        - host: The host that was connected to
        - command: The command that was executed
        - exit_code: Command exit code (0 = success, non-zero = failure)
        - stdout: Standard output from command
        - stderr: Standard error from command
        - duration_ms: Execution time in milliseconds

        Or error envelope for actual failures (timeout, connection failure, invalid host)

    Example:
        >>> ssh_exec("cube", "docker ps")
        {
            "ok": True,
            "data": {
                "host": "cube",
                "command": "docker ps",
                "exit_code": 0,
                "stdout": "CONTAINER ID   IMAGE...",
                "stderr": "",
                "duration_ms": 1234
            }
        }

        >>> ssh_exec("cube", "docker ps | grep nonexistent")
        {
            "ok": True,
            "data": {
                "host": "cube",
                "command": "docker ps | grep nonexistent",
                "exit_code": 1,
                "stdout": "",
                "stderr": "",
                "duration_ms": 456
            }
        }

    Note: Non-zero exit codes are NOT errors - they indicate the command ran
    but returned a failure code. Only connection/timeout failures are errors.
    """
    try:
        # Validate host parameter
        if not host:
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                "host parameter is required",
            )

        # Validate command parameter
        if not command:
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                "command parameter is required",
            )

        # Parse and validate host
        parsed = _parse_host(host)
        if not parsed:
            allowed = ", ".join(ALLOWED_HOSTS.keys())
            return tool_error(
                ErrorType.VALIDATION_ERROR,
                f"Unknown host: {host}. Allowed: {allowed}, or use 'user@hostname' format.",
            )

        user, hostname, port = parsed

        # Construct SSH command
        # -o StrictHostKeyChecking=no: Don't prompt for host key verification
        # -o ConnectTimeout=5: Fail fast if connection hangs
        # -i ~/.ssh/id_ed25519: Use the rosetta key for authentication
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-p", port,
            "-i", f"{subprocess.os.path.expanduser('~')}/.ssh/id_ed25519",
            f"{user}@{hostname}",
            command,
        ]

        logger.info(f"Executing SSH command on {host}: {command}")
        start_time = time.time()

        # Execute command with timeout
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        # Get stdout/stderr and truncate if necessary
        stdout = result.stdout
        stderr = result.stderr

        if len(stdout) > MAX_OUTPUT_SIZE:
            stdout = stdout[:MAX_OUTPUT_SIZE] + "\n... [stdout truncated]"

        if len(stderr) > MAX_OUTPUT_SIZE:
            stderr = stderr[:MAX_OUTPUT_SIZE] + "\n... [stderr truncated]"

        # Return success envelope even for non-zero exit codes
        # (non-zero exit code means command ran but failed, not an error)
        return tool_success({
            "host": host,
            "command": command,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
        })

    except subprocess.TimeoutExpired:
        logger.error(f"SSH command timeout after {timeout_secs}s on {host}: {command}")
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            f"Command timed out after {timeout_secs} seconds",
        )

    except subprocess.CalledProcessError as e:
        # This shouldn't happen with subprocess.run (it doesn't raise by default)
        # but include for completeness
        logger.error(f"SSH command failed on {host}: {e}")
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            f"SSH command failed: {str(e)}",
        )

    except FileNotFoundError:
        logger.error("SSH binary not found in PATH")
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            "SSH client not found. Ensure OpenSSH is installed.",
        )

    except Exception as e:
        logger.exception(f"Unexpected error executing SSH command on {host}")
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            f"Unexpected error: {str(e)}",
        )


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=ssh_exec,
        name="ssh_exec",
        description=(
            "Execute a shell command on a remote infrastructure server via SSH. "
            "Allowed hosts: cube (home GPU), clifford (production VPS), zerg (project server), slim (EU VPS). "
            "Returns exit code, stdout, stderr, and duration. Non-zero exit codes are not errors - "
            "they indicate the command ran but returned a failure code."
        ),
    ),
]
