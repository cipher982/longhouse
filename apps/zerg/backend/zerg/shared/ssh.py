"""SSH utilities for remote command execution.

Ported from Sauron for use in scheduled jobs.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# SSH configuration
SSH_KEY_PATH = Path.home() / ".ssh" / "id_rsa"
DEFAULT_TIMEOUT = 30  # seconds


class SSHResult(NamedTuple):
    """Result of SSH command execution."""

    success: bool
    stdout: str
    stderr: str
    returncode: int


def run_ssh_command(
    host: str,
    command: str,
    *,
    user: str = "root",
    port: int = 22,
    timeout: int = DEFAULT_TIMEOUT,
    key_path: Path | None = None,
    bastion_host: str | None = None,
) -> SSHResult:
    """
    Execute a command on a remote host via SSH.

    Args:
        host: Target hostname (e.g., 'clifford' via Tailscale MagicDNS)
        command: Command to execute on remote host
        user: SSH user (default: root)
        port: SSH port (default: 22)
        timeout: Command timeout in seconds
        key_path: Path to SSH private key (default: ~/.ssh/id_rsa)
        bastion_host: Optional jump host for ProxyJump (e.g., 'root@clifford')

    Returns:
        SSHResult with success status, stdout, stderr, and returncode

    Example:
        result = run_ssh_command(
            "clifford",
            "kopia snapshot list / --json",
            timeout=60
        )
        if result.success:
            print(result.stdout)

        # SSH via jump host on custom port
        result = run_ssh_command(
            "cube",
            "echo ok",
            port=2222,
            bastion_host="root@clifford"
        )
    """
    key_path = key_path or SSH_KEY_PATH

    if not key_path.exists():
        logger.error("SSH key not found: %s", key_path)
        return SSHResult(
            success=False,
            stdout="",
            stderr=f"SSH key not found: {key_path}",
            returncode=-1,
        )

    ssh_command = [
        "ssh",
        "-i",
        str(key_path),
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",  # Fail if password/passphrase needed
        "-o",
        "StrictHostKeyChecking=no",  # Skip host key verification for Tailscale IPs
        "-o",
        "ConnectTimeout=10",  # Connection timeout
        "-o",
        "ServerAliveInterval=5",  # Keep connection alive
        "-o",
        "ServerAliveCountMax=3",  # Max failures before disconnect
    ]

    # Add ProxyJump if bastion host specified
    if bastion_host:
        ssh_command.extend(["-J", bastion_host])

    ssh_command.extend([f"{user}@{host}", command])

    try:
        logger.debug("Executing SSH command on %s@%s: %s", user, host, command)
        result = subprocess.run(
            ssh_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # Don't raise on non-zero exit
        )

        success = result.returncode == 0
        if not success:
            stderr_preview = result.stderr[:200] if result.stderr else ""
            logger.warning("SSH failed on %s: rc=%d, %s", host, result.returncode, stderr_preview)

        return SSHResult(
            success=success,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )

    except subprocess.TimeoutExpired:
        logger.error("SSH command timed out after %ds on %s", timeout, host)
        return SSHResult(
            success=False,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            returncode=-1,
        )

    except Exception as e:
        logger.exception("SSH command failed on %s: %s", host, e)
        return SSHResult(
            success=False,
            stdout="",
            stderr=str(e),
            returncode=-1,
        )


def test_ssh_connection(
    host: str,
    user: str = "root",
    port: int = 22,
    timeout: int = 10,
    bastion_host: str | None = None,
) -> bool:
    """
    Test SSH connectivity to a host.

    Args:
        host: Target hostname
        user: SSH user
        port: SSH port
        timeout: Connection timeout
        bastion_host: Optional jump host for ProxyJump

    Returns:
        True if connection successful, False otherwise
    """
    result = run_ssh_command(host, "echo ok", user=user, port=port, timeout=timeout, bastion_host=bastion_host)
    return result.success and result.stdout.strip() == "ok"
