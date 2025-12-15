"""Command validation service for runner capability enforcement.

Validates commands against runner capabilities to ensure safety and prevent
unauthorized or dangerous operations. Implements defense-in-depth by enforcing
restrictions both server-side (routing) and runner-side (execution gate).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


class CommandValidator:
    """Validates commands against runner capabilities.

    Provides strict validation for exec.readonly mode and flexible validation
    for exec.full mode. Implements allowlists for safe commands and blocklists
    for dangerous operations.
    """

    # Shell metacharacters that indicate complex/dangerous commands
    FORBIDDEN_CHARS = {';', '|', '&', '>', '<', '$', '(', ')', '`', '\n', '\\'}

    # Allowlist for exec.readonly (argv[0] patterns)
    READONLY_ALLOWLIST = {
        # System read-only commands
        'uname',
        'uptime',
        'date',
        'whoami',
        'id',
        'df',
        'du',
        'free',
        'ps',
        'top',
        'hostname',
        'cat',
        'head',
        'tail',
        'ls',
        'pwd',
        'env',
        'printenv',
        'echo',  # safe read-only command for testing/debugging
        'false',  # safe command (exits with 1)
        'true',  # safe command (exits with 0)
        # Commands requiring subcommand validation
        'systemctl',  # only 'status' subcommand
        'journalctl',  # only with --no-pager
        'docker',  # only if docker capability, only read-only subcommands
    }

    # Docker read-only subcommands
    DOCKER_READONLY_SUBCOMMANDS = {
        'ps',
        'logs',
        'stats',
        'inspect',
        'images',
        'info',
        'version',
    }

    # Explicitly denied commands (blocklist)
    DESTRUCTIVE_COMMANDS = {
        'rm',
        'rmdir',
        'mkfs',
        'dd',
        'shutdown',
        'reboot',
        'halt',
        'poweroff',
        'useradd',
        'userdel',
        'usermod',
        'groupadd',
        'passwd',
        'chmod',
        'chown',
        'chgrp',
        'iptables',
        'ip6tables',
        'ufw',
        'firewall-cmd',
        'mount',
        'umount',
        'fdisk',
        'parted',
        'kill',
        'killall',
        'pkill',
    }

    def validate(
        self, command: str, capabilities: list[str]
    ) -> tuple[bool, Optional[str]]:
        """Validate command against capabilities.

        Args:
            command: Shell command to validate
            capabilities: List of runner capabilities (e.g., ["exec.readonly", "docker"])

        Returns:
            Tuple of (allowed, reason):
            - (True, None) if allowed
            - (False, "reason") if denied
        """
        # exec.full allows everything
        if "exec.full" in capabilities:
            logger.debug(f"Command allowed via exec.full capability")
            return (True, None)

        # exec.readonly requires strict validation
        return self._validate_readonly(command, capabilities)

    def _has_shell_metacharacters(self, command: str) -> bool:
        """Check for forbidden shell metacharacters.

        Args:
            command: Command to check

        Returns:
            True if command contains forbidden characters
        """
        return any(char in command for char in self.FORBIDDEN_CHARS)

    def _parse_argv0(self, command: str) -> str:
        """Extract the base command (argv[0]).

        Args:
            command: Full command string

        Returns:
            Base command name (first token)
        """
        # Strip leading whitespace and extract first token
        tokens = command.strip().split()
        if not tokens:
            return ""

        # Handle absolute paths (e.g., /usr/bin/docker -> docker)
        base_cmd = tokens[0]
        if "/" in base_cmd:
            base_cmd = base_cmd.split("/")[-1]

        return base_cmd

    def _validate_readonly(
        self, command: str, capabilities: list[str]
    ) -> tuple[bool, Optional[str]]:
        """Validate against exec.readonly allowlist.

        Args:
            command: Command to validate
            capabilities: Runner capabilities

        Returns:
            Tuple of (allowed, reason)
        """
        # Check for shell metacharacters
        if self._has_shell_metacharacters(command):
            return (
                False,
                "Command contains shell metacharacters (pipes, redirects, etc). "
                "These are not allowed in exec.readonly mode.",
            )

        # Extract base command
        argv0 = self._parse_argv0(command)
        if not argv0:
            return (False, "Empty command")

        # Check destructive commands blocklist
        if argv0 in self.DESTRUCTIVE_COMMANDS:
            return (
                False,
                f"Command '{argv0}' is explicitly blocked (destructive operation)",
            )

        # Check allowlist
        if argv0 not in self.READONLY_ALLOWLIST:
            return (
                False,
                f"Command '{argv0}' is not in the readonly allowlist. "
                "Grant exec.full capability to run arbitrary commands.",
            )

        # Special validation for specific commands
        if argv0 == "systemctl":
            if not self._validate_systemctl(command):
                return (
                    False,
                    "systemctl is only allowed with 'status' subcommand in readonly mode",
                )

        elif argv0 == "journalctl":
            if not self._validate_journalctl(command):
                return (
                    False,
                    "journalctl must include --no-pager flag in readonly mode (prevents hanging)",
                )

        elif argv0 == "docker":
            # Docker requires explicit capability
            if "docker" not in capabilities:
                return (
                    False,
                    "docker command requires 'docker' capability. "
                    "Runner must be started with docker.sock mount and docker capability must be granted.",
                )

            # Validate docker subcommand is read-only
            if not self._validate_docker(command):
                return (
                    False,
                    f"docker subcommand is not allowed in readonly mode. "
                    f"Allowed: {', '.join(sorted(self.DOCKER_READONLY_SUBCOMMANDS))}",
                )

        # Command passed all checks
        return (True, None)

    def _validate_systemctl(self, command: str) -> bool:
        """Validate systemctl command - only allow 'status' subcommand.

        Args:
            command: Full command string

        Returns:
            True if valid systemctl command
        """
        tokens = command.strip().split()
        if len(tokens) < 2:
            return False

        # Second token should be 'status'
        return tokens[1] == "status"

    def _validate_journalctl(self, command: str) -> bool:
        """Validate journalctl command - must include --no-pager.

        Args:
            command: Full command string

        Returns:
            True if valid journalctl command
        """
        # Must include --no-pager flag to prevent hanging
        return "--no-pager" in command

    def _validate_docker(self, command: str) -> bool:
        """Validate docker command - only allow read-only subcommands.

        Args:
            command: Full command string

        Returns:
            True if valid docker command
        """
        tokens = command.strip().split()
        if len(tokens) < 2:
            return False

        # Extract subcommand (second token)
        subcommand = tokens[1]

        # Check if subcommand is in readonly list
        return subcommand in self.DOCKER_READONLY_SUBCOMMANDS
