"""Core subprocess execution for agent CLIs."""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.libs.agent_runner.backends import Backend
from zerg.libs.agent_runner.backends import get_config
from zerg.libs.agent_runner.context import detect_context


@dataclass
class AgentResult:
    """Result from running an agent."""

    ok: bool
    output: str
    exit_code: int
    duration_ms: int
    error: str | None = None
    stderr: str | None = None

    @property
    def status(self) -> str:
        """Simple status string."""
        if self.ok:
            return "ok"
        if self.exit_code == -1:
            return "timeout"
        if self.exit_code == -2:
            return "not_found"
        return "error"


def _run_subprocess(
    cmd: list[str],
    stdin_data: bytes | None,
    env: dict[str, str],
    cwd: str | None,
    timeout_s: int,
) -> tuple[str, str, int, bool]:
    """Run subprocess with proper timeout and cleanup.

    Returns: (stdout, stderr, return_code, timed_out)
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_data else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        text=True,
    )

    try:
        stdout, stderr = proc.communicate(
            input=stdin_data.decode() if stdin_data else None,
            timeout=timeout_s,
        )
        return stdout or "", stderr or "", proc.returncode, False
    except subprocess.TimeoutExpired:
        # Kill the process tree on timeout
        proc.kill()
        proc.wait()
        return "", "", -1, True


async def run(
    prompt: str,
    backend: Backend,
    *,
    cwd: str | Path | None = None,
    timeout_s: int = 300,
    **backend_kwargs: Any,
) -> AgentResult:
    """Run an agent CLI and return the result.

    Args:
        prompt: The prompt to send to the agent
        backend: Which backend to use (zai, bedrock, codex, gemini)
        cwd: Working directory for the agent
        timeout_s: Timeout in seconds (default 5 minutes)
        **backend_kwargs: Backend-specific options (api_key, model, etc.)

    Returns:
        AgentResult with ok, output, duration_ms, error, etc.
    """
    ctx = detect_context()
    config = get_config(backend, prompt, ctx, **backend_kwargs)
    env = config.build_env()

    cwd_str = str(cwd) if cwd else None
    start = time.monotonic()

    try:
        stdout, stderr, return_code, timed_out = await asyncio.to_thread(
            _run_subprocess,
            config.cmd,
            config.stdin_data,
            env,
            cwd_str,
            timeout_s,
        )

        duration_ms = int((time.monotonic() - start) * 1000)

        if timed_out:
            return AgentResult(
                ok=False,
                output="",
                exit_code=-1,  # Special code for timeout
                duration_ms=duration_ms,
                error=f"Agent timed out after {timeout_s}s",
            )

        if return_code != 0:
            return AgentResult(
                ok=False,
                output=stdout,
                exit_code=return_code,
                duration_ms=duration_ms,
                error=stderr or f"Exit code {return_code}",
                stderr=stderr,
            )

        if not stdout.strip():
            return AgentResult(
                ok=False,
                output="",
                exit_code=0,
                duration_ms=duration_ms,
                error="Empty output from agent",
                stderr=stderr,
            )

        return AgentResult(
            ok=True,
            output=stdout,
            exit_code=0,
            duration_ms=duration_ms,
            stderr=stderr,
        )

    except FileNotFoundError as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        return AgentResult(
            ok=False,
            output="",
            exit_code=-2,  # Special code for not found
            duration_ms=duration_ms,
            error=f"CLI not found: {e}",
        )

    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        return AgentResult(
            ok=False,
            output="",
            exit_code=-3,
            duration_ms=duration_ms,
            error=str(e),
        )
