"""Cloud Executor – run headless agent execution via subprocess.

This service runs agents as headless subprocesses using the `agent-run` CLI tool.
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
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default timeout for agent execution (1 hour)
DEFAULT_TIMEOUT_SECONDS = 3600

# Default model for cloud execution
DEFAULT_CLOUD_MODEL = "bedrock/claude-sonnet"

# Model ID mapping from Zerg model IDs to agent-run provider/model format
MODEL_MAPPING = {
    # OpenAI models
    "gpt-5": "openai/gpt-5",
    "gpt-5.2": "openai/gpt-5.2",
    "gpt-5-mini": "openai/gpt-5-mini",
    "gpt-4o": "openai/gpt-4o",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    # Bedrock/Anthropic models
    "claude-sonnet": "bedrock/claude-sonnet",
    "claude-opus": "bedrock/claude-opus",
    "claude-haiku": "bedrock/claude-haiku",
    # Already prefixed - pass through
}


def normalize_model_id(model: str) -> str:
    """Convert Zerg model ID to agent-run provider/model format.

    If model already has a provider prefix (contains '/'), returns as-is.
    Otherwise, looks up in MODEL_MAPPING or defaults to openai/ prefix.
    """
    if "/" in model:
        return model  # Already has provider prefix

    if model in MODEL_MAPPING:
        return MODEL_MAPPING[model]

    # Default to openai/ for unknown models
    return f"openai/{model}"


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
    """Execute agents as headless subprocesses using agent-run CLI."""

    def __init__(
        self,
        agent_run_path: str | None = None,
        default_model: str = DEFAULT_CLOUD_MODEL,
    ):
        """Initialize the cloud executor.

        Parameters
        ----------
        agent_run_path
            Path to agent-run executable. If None, uses 'agent-run' from PATH.
        default_model
            Default model to use if not specified in run_agent().
        """
        self.agent_run_path = agent_run_path or "agent-run"
        self.default_model = default_model

    async def run_agent(
        self,
        task: str,
        workspace_path: str | Path,
        *,
        model: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        env_vars: dict[str, str] | None = None,
    ) -> CloudExecutionResult:
        """Execute a task using agent-run in the given workspace.

        This method:
        1. Spawns agent-run subprocess in the workspace directory
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

        Returns
        -------
        CloudExecutionResult
            Structured result with output, status, timing info

        Notes
        -----
        The agent-run command is expected to be available on the system PATH
        or at the configured agent_run_path. On zerg-vps, this is typically
        installed via the agent-mesh MCP tools.
        """
        workspace = Path(workspace_path)
        # Normalize model ID to provider/model format for agent-run
        raw_model = model or self.default_model
        effective_model = normalize_model_id(raw_model)
        started_at = datetime.now(timezone.utc)

        logger.info(f"Starting cloud execution in {workspace} with model {effective_model}")
        logger.debug(f"Task: {task[:200]}...")

        # Build command
        # agent-run -m <model> "<prompt>"
        cmd = [
            self.agent_run_path,
            "-m",
            effective_model,
            task,
        ]

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
                model=effective_model,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )

        try:
            # Create subprocess
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                proc.kill()
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
                    model=effective_model,
                    started_at=started_at,
                    finished_at=finished_at,
                )

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
                    model=effective_model,
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
                    model=effective_model,
                    started_at=started_at,
                    finished_at=finished_at,
                )

        except FileNotFoundError:
            logger.error(f"agent-run not found at: {self.agent_run_path}")
            return CloudExecutionResult(
                status="failed",
                output="",
                error=f"agent-run executable not found at: {self.agent_run_path}",
                exit_code=-1,
                model=effective_model,
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
                model=effective_model,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )

    async def check_agent_run_available(self) -> tuple[bool, str]:
        """Check if agent-run is available and working.

        Returns
        -------
        tuple[bool, str]
            (available, message) - whether agent-run is available and any message
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self.agent_run_path,
                "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode == 0:
                return True, "agent-run is available"
            else:
                error = stderr.decode() if stderr else "Unknown error"
                return False, f"agent-run returned error: {error}"

        except FileNotFoundError:
            return False, f"agent-run not found at: {self.agent_run_path}"
        except asyncio.TimeoutError:
            return False, "agent-run --help timed out"
        except Exception as e:
            return False, f"Error checking agent-run: {e}"


__all__ = ["CloudExecutor", "CloudExecutionResult"]
