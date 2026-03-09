"""SSH utilities for Longhouse scheduled jobs.

Used by builtin product jobs and optional external job packs when a jobs repo is
explicitly configured. This module is separate from the standalone Sauron service.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
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


@contextmanager
def _resolve_key(
    key_path: Path | None,
    key_content: str | None,
) -> Generator[Path, None, None]:
    """Resolve the SSH key to use, yielding a path.

    Priority:
    1. ``key_content`` argument (raw PEM string) — written to a tempfile
    2. ``SSH_PRIVATE_KEY`` env var (raw PEM string) — written to a tempfile.
       Note: ``SSH_PRIVATE_KEY`` must be a raw PEM string. Many secret UIs
       store newlines as literal ``\\n`` — normalize before storing.
    3. ``key_path`` argument
    4. Default ``~/.ssh/id_rsa``

    Tempfiles are cleaned up on context exit so callers never need to manage them.

    Raises:
        ValueError: If ``key_content`` is an empty/whitespace-only string,
            which likely indicates a misconfigured secret rather than intent
            to fall back to the file-based key.
    """
    if key_content is not None:
        if not key_content.strip():
            raise ValueError("key_content was provided but is empty — check the secret value")
        content = key_content
    else:
        content = os.environ.get("SSH_PRIVATE_KEY")

    if content:
        # Normalize literal \n sequences written by secret management UIs
        normalized = content.replace("\\n", "\n")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
            f.write(normalized)
            tmp = Path(f.name)
        try:
            tmp.chmod(0o600)
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)
        return

    resolved = key_path or SSH_KEY_PATH
    yield resolved


def run_ssh_command(
    host: str,
    command: str,
    *,
    user: str = "root",
    port: int = 22,
    timeout: int = DEFAULT_TIMEOUT,
    key_path: Path | None = None,
    key_content: str | None = None,
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
        key_content: Raw PEM key string. Takes priority over key_path and the
            SSH_PRIVATE_KEY env var. Intended for new-style jobs that receive
            the key via ``ctx.require_secret("SSH_PRIVATE_KEY")``.
        bastion_host: Optional jump host for ProxyJump (e.g., 'root@clifford')

    Returns:
        SSHResult with success status, stdout, stderr, and returncode

    Key resolution order:
        1. key_content argument
        2. SSH_PRIVATE_KEY env var
        3. key_path argument
        4. ~/.ssh/id_rsa

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

        # New-style job with per-user secret
        async def run(ctx: JobContext):
            result = run_ssh_command(
                "myserver",
                "uptime",
                key_content=ctx.require_secret("SSH_PRIVATE_KEY"),
            )
    """
    try:
        key_ctx = _resolve_key(key_path, key_content)
    except ValueError as e:
        return SSHResult(success=False, stdout="", stderr=str(e), returncode=-1)

    with key_ctx as resolved_key:
        if not resolved_key.exists():
            logger.error("SSH key not found: %s", resolved_key)
            return SSHResult(
                success=False,
                stdout="",
                stderr=f"SSH key not found: {resolved_key}",
                returncode=-1,
            )

        ssh_command = [
            "ssh",
            "-i",
            str(resolved_key),
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
    key_path: Path | None = None,
    key_content: str | None = None,
    bastion_host: str | None = None,
) -> bool:
    """
    Test SSH connectivity to a host.

    Args:
        host: Target hostname
        user: SSH user
        port: SSH port
        timeout: Connection timeout
        key_path: Path to SSH private key
        key_content: Raw PEM key string (takes priority over key_path)
        bastion_host: Optional jump host for ProxyJump

    Returns:
        True if connection successful, False otherwise
    """
    result = run_ssh_command(
        host,
        "echo ok",
        user=user,
        port=port,
        timeout=timeout,
        key_path=key_path,
        key_content=key_content,
        bastion_host=bastion_host,
    )
    return result.success and result.stdout.strip() == "ok"
