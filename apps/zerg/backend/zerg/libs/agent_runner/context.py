"""Environment detection for agent execution.

Detects container vs laptop context and adjusts settings accordingly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class ExecutionContext:
    """Describes the current execution environment."""

    in_container: bool
    home_writable: bool

    @property
    def effective_home(self) -> str:
        """Return HOME to use for agents that need writable home."""
        if self.in_container and not self.home_writable:
            return "/tmp"
        return os.environ.get("HOME", "/tmp")


@lru_cache(maxsize=1)
def detect_context() -> ExecutionContext:
    """Detect current execution context.

    Container detection:
    - /.dockerenv file exists (Docker)
    - /run/.containerenv exists (Podman)
    - cgroup contains docker/containerd/kubepods

    Home writable check:
    - Tries to create a temp file in HOME
    """
    in_container = _detect_container()
    home_writable = _check_home_writable()

    return ExecutionContext(
        in_container=in_container,
        home_writable=home_writable,
    )


def _detect_container() -> bool:
    """Check if running inside a container."""
    # Docker
    if os.path.exists("/.dockerenv"):
        return True

    # Podman
    if os.path.exists("/run/.containerenv"):
        return True

    # cgroup check for kubernetes/docker
    try:
        with open("/proc/1/cgroup") as f:
            cgroup = f.read()
            if any(x in cgroup for x in ("docker", "containerd", "kubepods")):
                return True
    except (FileNotFoundError, PermissionError):
        pass

    return False


def _check_home_writable() -> bool:
    """Check if HOME directory is writable."""
    home = os.environ.get("HOME", "/root")
    try:
        test_file = Path(home) / ".agent_runner_test"
        test_file.write_text("test")
        test_file.unlink()
        return True
    except (PermissionError, OSError):
        return False
