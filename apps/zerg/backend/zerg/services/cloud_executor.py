"""Cloud Executor – run headless agent execution via subprocess.

This service runs agents as headless subprocesses using the `hatch` CLI tool.
It enables 24/7 cloud execution independent of laptop connectivity.

Usage:
    executor = CloudExecutor()
    result = await executor.run_agent(
        task="Fix the typo in README.md",
        workspace_path="/var/jarvis/workspaces/run-123",
        model="bedrock/claude-sonnet",
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default timeout for agent execution (1 hour)
DEFAULT_TIMEOUT_SECONDS = 3600

# Default model for cloud execution (backend/model format)
# Using z.ai to avoid AWS SSO credential complexity
DEFAULT_CLOUD_MODEL = "zai/glm-4.7"

# Model ID mapping from Zerg model IDs to hatch backend/model format
MODEL_MAPPING = {
    # OpenAI models -> codex backend
    "gpt-5": "codex/gpt-5",
    "gpt-5.2": "codex/gpt-5.2",
    "gpt-5-mini": "codex/gpt-5-mini",
    "gpt-4o": "codex/gpt-4o",
    "gpt-4o-mini": "codex/gpt-4o-mini",
    # Bedrock/Anthropic models
    "claude-sonnet": "bedrock/claude-sonnet",
    "claude-opus": "bedrock/claude-opus",
    "claude-haiku": "bedrock/claude-haiku",
    # z.ai models
    "glm-4.7": "zai/glm-4.7",
    # Gemini models
    "gemini-pro": "gemini/gemini-pro",
}


def normalize_model_id(model: str) -> tuple[str, str]:
    """Convert Zerg model ID to hatch backend and model name.

    Returns (backend, model_name) tuple suitable for hatch CLI args.
    If model already has a provider prefix (contains '/'), parses it.
    Otherwise, looks up in MODEL_MAPPING or defaults to zai backend.
    """
    if "/" in model:
        backend, model_name = model.split("/", 1)
        return backend, model_name

    if model in MODEL_MAPPING:
        backend, model_name = MODEL_MAPPING[model].split("/", 1)
        return backend, model_name

    # Default to zai backend for unknown models
    return "zai", model


@dataclass
class CloudExecutionResult:
    """Result from cloud agent execution."""

    status: str  # "success", "failed", "timeout"
    output: str  # stdout from agent
    error: str | None = None  # stderr or error message
    exit_code: int = 0
    duration_ms: int = 0
    model: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class CloudExecutor:
    """Execute agents as headless subprocesses using hatch CLI."""

    def __init__(
        self,
        hatch_path: str | None = None,
        default_model: str = DEFAULT_CLOUD_MODEL,
    ):
        """Initialize the cloud executor.

        Parameters
        ----------
        hatch_path
            Path to hatch executable. If None, uses 'hatch' from PATH.
        default_model
            Default model to use if not specified in run_agent().
        """
        self.hatch_path = hatch_path or "hatch"
        self.default_model = default_model

    async def run_agent(
        self,
        task: str,
        workspace_path: str | Path,
        *,
        model: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        env_vars: dict[str, str] | None = None,
        resume_session_id: str | None = None,
    ) -> CloudExecutionResult:
        """Execute a task using hatch in the given workspace.

        This method:
        1. Spawns hatch subprocess in the workspace directory
        2. Captures stdout/stderr
        3. Enforces timeout
        4. Returns structured result

        Parameters
        ----------
        task
            Natural language task for the agent to execute
        workspace_path
            Directory where agent should run (working directory)
        model
            LLM model to use (default: bedrock/claude-sonnet)
        timeout
            Maximum execution time in seconds (default: 3600 = 1 hour)
        env_vars
            Additional environment variables for the subprocess
        resume_session_id
            Claude Code session ID to resume (passed to hatch --resume)

        Returns
        -------
        CloudExecutionResult
            Structured result with output, status, timing info

        Notes
        -----
        The hatch command is expected to be available on the system PATH
        or at the configured hatch_path. On zerg-vps, installed via uv tool.
        """
        workspace = Path(workspace_path)
        # Normalize model ID to backend/model tuple for hatch
        raw_model = model or self.default_model
        backend, model_name = normalize_model_id(raw_model)
        started_at = datetime.now(timezone.utc)

        logger.info(f"Starting cloud execution in {workspace} with backend={backend}, model={model_name}")
        logger.debug(f"Task: {task[:200]}...")

        # Build command for hatch CLI
        # hatch -b <backend> --model <model> -C <workspace> [--resume <id>] "<prompt>"
        cmd = [
            self.hatch_path,
            "-b",
            backend,
            "--model",
            model_name,
            "-C",
            str(workspace),
        ]

        # Add resume flag for session continuity (Claude backends only)
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])

        cmd.append(task)

        # Prepare environment
        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)

        # Ensure workspace exists
        if not workspace.exists():
            return CloudExecutionResult(
                status="failed",
                output="",
                error=f"Workspace directory does not exist: {workspace}",
                exit_code=-1,
                model=f"{backend}/{model_name}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )

        try:
            # Create subprocess in a new session/process group
            # This allows us to kill the entire process tree on timeout
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,  # Creates new process group for cleanup
            )

            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill the entire process group on timeout to prevent orphan children
                # This is critical because hatch may spawn child processes
                self._kill_process_group(proc)
                await proc.wait()
                finished_at = datetime.now(timezone.utc)
                duration_ms = int((finished_at - started_at).total_seconds() * 1000)

                logger.warning(f"Cloud execution timed out after {timeout}s in {workspace}")

                return CloudExecutionResult(
                    status="timeout",
                    output="",
                    error=f"Execution timed out after {timeout} seconds",
                    exit_code=-1,
                    duration_ms=duration_ms,
                    model=f"{backend}/{model_name}",
                    started_at=started_at,
                    finished_at=finished_at,
                )
            except asyncio.CancelledError:
                # Task was cancelled (shutdown, explicit cancellation)
                # Kill the process group to avoid orphan processes before re-raising
                self._kill_process_group(proc)
                # Wait for process to be reaped to prevent zombie processes
                await proc.wait()
                logger.warning(f"Cloud execution cancelled in {workspace}, killed process group")
                raise  # Propagate cancellation

            finished_at = datetime.now(timezone.utc)
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            error_output = stderr.decode("utf-8", errors="replace") if stderr else ""

            if proc.returncode == 0:
                logger.info(f"Cloud execution completed successfully in {duration_ms}ms")
                return CloudExecutionResult(
                    status="success",
                    output=output,
                    error=error_output if error_output else None,
                    exit_code=proc.returncode,
                    duration_ms=duration_ms,
                    model=f"{backend}/{model_name}",
                    started_at=started_at,
                    finished_at=finished_at,
                )
            else:
                logger.warning(f"Cloud execution failed with exit code {proc.returncode}")
                return CloudExecutionResult(
                    status="failed",
                    output=output,
                    error=error_output or f"Exit code: {proc.returncode}",
                    exit_code=proc.returncode,
                    duration_ms=duration_ms,
                    model=f"{backend}/{model_name}",
                    started_at=started_at,
                    finished_at=finished_at,
                )

        except FileNotFoundError:
            logger.error(f"hatch not found at: {self.hatch_path}")
            return CloudExecutionResult(
                status="failed",
                output="",
                error=f"hatch executable not found at: {self.hatch_path}",
                exit_code=-1,
                model=f"{backend}/{model_name}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.exception(f"Cloud execution failed: {e}")
            return CloudExecutionResult(
                status="failed",
                output="",
                error=str(e),
                exit_code=-1,
                model=f"{backend}/{model_name}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )

    def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the entire process group to prevent orphan children.

        This is critical because hatch may spawn child processes that would
        otherwise be left running if we only kill the parent.
        """
        try:
            # Kill process group (negative PID targets the group)
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError) as e:
            # Process may have already exited
            logger.debug(f"Process group kill failed (may have already exited): {e}")
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def check_hatch_available(self) -> tuple[bool, str]:
        """Check if hatch is available and working.

        Returns
        -------
        tuple[bool, str]
            (available, message) - whether hatch is available and any message
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self.hatch_path,
                "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode == 0:
                return True, "hatch is available"
            else:
                error = stderr.decode() if stderr else "Unknown error"
                return False, f"hatch returned error: {error}"

        except FileNotFoundError:
            return False, f"hatch not found at: {self.hatch_path}"
        except asyncio.TimeoutError:
            return False, "hatch --help timed out"
        except Exception as e:
            return False, f"Error checking hatch: {e}"


__all__ = ["CloudExecutor", "CloudExecutionResult"]
