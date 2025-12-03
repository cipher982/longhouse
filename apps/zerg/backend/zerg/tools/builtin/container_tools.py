"""Container-backed shell tool (read-only by default).

This tool executes a string command inside an ephemeral, constrained
container using the internal ContainerRunner. Network is disabled by
default; root filesystem is read-only; a writable workspace is mounted
at /workspace.

Keep this minimal; policy/approvals are enforced at the product layer
by allowlists and per-agent tool scopes.
"""

from __future__ import annotations

from typing import Dict

from langchain_core.tools import StructuredTool

from zerg.config import get_settings


def _container_exec(command: str, timeout_secs: int = 20) -> str:
    """Run command inside ephemeral container and return combined logs.

    Args:
        command: Shell command string executed via /bin/sh -lc
        timeout_secs: Max seconds to wait before killing the container

    Returns:
        Combined stdout/stderr text with an exit summary header.
    """

    try:  # Import lazily to avoid docker dependency at import time
        from zerg.services.container_runner import ContainerRunner
        from zerg.services.container_runner import ContainerRunnerConfig
    except Exception as exc:  # pragma: no cover
        return f"Error: container runner unavailable ({exc})"

    if not get_settings().container_tools_enabled:
        return "Error: container tools are disabled by policy"

    runner = ContainerRunner(ContainerRunnerConfig.from_env())
    result: Dict[str, object] = runner.run(["/bin/sh", "-lc", command], timeout_secs=timeout_secs)

    logs = str(result.get("stdout", ""))
    if len(logs) > 10000:
        logs = logs[:10000] + "\n... [truncated]"

    exit_code = result.get("exit_code", 1)
    duration_ms = result.get("duration_ms", None)
    header = f"exit_code={exit_code}; duration_ms={duration_ms}"
    return header + "\n" + logs


TOOLS = [
    StructuredTool.from_function(
        _container_exec,
        name="container_exec",
        description=(
            "Execute a read-only shell command inside a hardened container. "
            "Args: command: str, timeout_secs: int (default 20)."
        ),
    )
]
