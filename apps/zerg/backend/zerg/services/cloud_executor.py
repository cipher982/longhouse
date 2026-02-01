"""Cloud Executor – run headless commis execution via subprocess or container.

This service runs commis as headless subprocesses using the `hatch` CLI tool.
It enables 24/7 cloud execution independent of laptop connectivity.

Supports two execution modes:
- subprocess (default): Direct hatch execution, fast, trusted environments
- sandbox: Docker container execution, isolated, for autonomous/scheduled tasks

Usage:
    executor = CloudExecutor()
    result = await executor.run_commis(
        task="Fix the typo in README.md",
        workspace_path="/var/oikos/workspaces/run-123",
        model="bedrock/claude-sonnet",
    )

    # For sandboxed execution:
    result = await executor.run_commis(
        task="Fix the typo in README.md",
        workspace_path="/var/oikos/workspaces/run-123",
        sandbox=True,
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default timeout for commis execution (1 hour)
DEFAULT_TIMEOUT_SECONDS = 3600

# Default model for cloud execution (backend/model format)
# Using z.ai to avoid AWS SSO credential complexity
DEFAULT_CLOUD_MODEL = "zai/glm-4.7"

# Workspace validation: only allow workspaces under this base directory
# This prevents arbitrary filesystem access via container mounts
WORKSPACE_BASE = Path(os.environ.get("COMMIS_WORKSPACE_BASE", "/var/oikos/workspaces"))

# Container image for sandboxed execution
SANDBOX_IMAGE = os.environ.get("COMMIS_SANDBOX_IMAGE", "ghcr.io/cipher982/commis-sandbox:v1")

CLAUDE_BACKENDS = {"zai", "bedrock"}
CLAUDE_OUTPUT_FORMAT_ENV = "HATCH_CLAUDE_OUTPUT_FORMAT"
CLAUDE_INCLUDE_PARTIAL_ENV = "HATCH_CLAUDE_INCLUDE_PARTIAL_MESSAGES"
ALLOWED_CLAUDE_OUTPUT_FORMATS = {"text", "json", "stream-json"}

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


def _coerce_bool(value: str | bool | None) -> bool:
    """Coerce common string/bool values to bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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


def validate_workspace_path(workspace_path: str | Path) -> Path:
    """Validate workspace is under allowed base directory.

    Security: Prevents arbitrary filesystem access via container mounts.
    The workspace must exist and be under WORKSPACE_BASE.

    Parameters
    ----------
    workspace_path
        Path to validate

    Returns
    -------
    Path
        Validated, resolved absolute path

    Raises
    ------
    ValueError
        If workspace is not under WORKSPACE_BASE or doesn't exist
    """
    path = Path(workspace_path).resolve()
    if not path.is_relative_to(WORKSPACE_BASE):
        raise ValueError(f"Workspace path must be under {WORKSPACE_BASE}, got: {path}")
    if not path.exists():
        raise ValueError(f"Workspace path does not exist: {path}")
    return path


def _sanitize_container_name(run_id: str) -> str:
    """Sanitize a run ID for use in Docker container name.

    Docker container names must match [a-zA-Z0-9][a-zA-Z0-9_.-]*
    """
    # Remove dashes and take first 12 chars
    safe = re.sub(r"[^a-zA-Z0-9]", "", run_id)[:12]
    # Add UUID suffix for uniqueness
    return f"commis-{safe}-{uuid.uuid4().hex[:8]}"


@dataclass
class CloudExecutionResult:
    """Result from cloud commis execution."""

    status: str  # "success", "failed", "timeout"
    output: str  # stdout from commis
    error: str | None = None  # stderr or error message
    exit_code: int = 0
    duration_ms: int = 0
    model: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class CloudExecutor:
    """Execute commis as headless subprocesses using hatch CLI."""

    def __init__(
        self,
        hatch_path: str | None = None,
        default_model: str = DEFAULT_CLOUD_MODEL,
        claude_output_format: str | None = None,
        include_partial_messages: bool | None = None,
    ):
        """Initialize the cloud executor.

        Parameters
        ----------
        hatch_path
            Path to hatch executable. If None, uses 'hatch' from PATH.
        default_model
            Default model to use if not specified in run_commis().
        claude_output_format
            Optional Claude output format override (text/json/stream-json).
            Defaults to env var HATCH_CLAUDE_OUTPUT_FORMAT when unset.
        include_partial_messages
            Include partial Claude messages (env: HATCH_CLAUDE_INCLUDE_PARTIAL_MESSAGES).
        """
        self.hatch_path = hatch_path or "hatch"
        self.default_model = default_model
        env_output_format = claude_output_format or os.environ.get(CLAUDE_OUTPUT_FORMAT_ENV)
        if env_output_format:
            env_output_format = env_output_format.strip()
            if env_output_format not in ALLOWED_CLAUDE_OUTPUT_FORMATS:
                logger.warning(
                    "Invalid Claude output format '%s' (allowed: %s). Ignoring.",
                    env_output_format,
                    ", ".join(sorted(ALLOWED_CLAUDE_OUTPUT_FORMATS)),
                )
                env_output_format = None
        self.claude_output_format = env_output_format
        self.include_partial_messages = _coerce_bool(
            include_partial_messages if include_partial_messages is not None else os.environ.get(CLAUDE_INCLUDE_PARTIAL_ENV)
        )

    async def run_commis(
        self,
        task: str,
        workspace_path: str | Path,
        *,
        model: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        env_vars: dict[str, str] | None = None,
        resume_session_id: str | None = None,
        sandbox: bool = False,
        run_id: str | None = None,
    ) -> CloudExecutionResult:
        """Execute a task using hatch in the given workspace.

        This method:
        1. Spawns hatch subprocess (or container if sandbox=True) in the workspace
        2. Captures stdout/stderr
        3. Enforces timeout
        4. Returns structured result

        Parameters
        ----------
        task
            Natural language task for the commis to execute
        workspace_path
            Directory where commis should run (working directory)
        model
            LLM model to use (default: bedrock/claude-sonnet)
        timeout
            Maximum execution time in seconds (default: 3600 = 1 hour)
        env_vars
            Additional environment variables for the subprocess
        resume_session_id
            Claude Code session ID to resume (passed to hatch --resume)
        sandbox
            If True, run in Docker container for isolation (default: False)
        run_id
            Optional run identifier for container naming (used when sandbox=True)

        Returns
        -------
        CloudExecutionResult
            Structured result with output, status, timing info

        Notes
        -----
        The hatch command is expected to be available on the system PATH
        or at the configured hatch_path. On zerg-vps, installed via uv tool.

        For sandbox=True, the workspace must be under WORKSPACE_BASE and Docker
        must be available. The container provides process/filesystem isolation
        but still has network access for LLM API calls.
        """
        # Route to container execution if sandbox mode requested
        if sandbox:
            return await self._run_in_container(
                task=task,
                workspace_path=workspace_path,
                model=model,
                timeout=timeout,
                env_vars=env_vars,
                run_id=run_id or "unknown",
            )

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

        if self.claude_output_format and backend in CLAUDE_BACKENDS:
            cmd.extend(["--output-format", self.claude_output_format])

        if self.include_partial_messages and backend in CLAUDE_BACKENDS:
            cmd.append("--include-partial-messages")

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

    async def _run_in_container(
        self,
        task: str,
        workspace_path: str | Path,
        run_id: str,
        *,
        model: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        env_vars: dict[str, str] | None = None,
    ) -> CloudExecutionResult:
        """Run commis in a sandboxed Docker container.

        Provides process and filesystem isolation for autonomous/scheduled tasks.
        The container has network access for LLM API calls but limited filesystem
        access (only the workspace is mounted).

        Security features:
        - Non-root user inside container
        - Dropped capabilities (CAP_DROP=ALL)
        - No new privileges (--security-opt=no-new-privileges)
        - Resource limits (memory, CPU, pids)
        - Workspace path validation (must be under WORKSPACE_BASE)

        Parameters
        ----------
        task
            Natural language task for the commis to execute
        workspace_path
            Directory to mount as /repo in the container
        run_id
            Run identifier for container naming
        model
            LLM model to use
        timeout
            Maximum execution time in seconds

        Returns
        -------
        CloudExecutionResult
            Structured result with output, status, timing info
        """
        raw_model = model or self.default_model
        backend, model_name = normalize_model_id(raw_model)
        started_at = datetime.now(timezone.utc)

        # Validate workspace path for security
        try:
            validated_path = validate_workspace_path(workspace_path)
        except ValueError as e:
            logger.error(f"Workspace validation failed: {e}")
            return CloudExecutionResult(
                status="failed",
                output="",
                error=str(e),
                exit_code=-1,
                model=f"{backend}/{model_name}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )

        # Generate unique container name
        container_name = _sanitize_container_name(run_id)

        # Get host UID/GID for workspace permissions
        workspace_stat = validated_path.stat()
        host_uid = workspace_stat.st_uid
        host_gid = workspace_stat.st_gid

        logger.info(f"Starting sandboxed execution in container {container_name} " f"with backend={backend}, model={model_name}")
        logger.debug(f"Task: {task[:200]}...")

        # Build docker run command
        # fmt: off
        cmd = [
            "docker", "run", "--rm",
            "-i",  # Keep stdin open for prompt
            "--name", container_name,
            # User mapping for workspace permissions
            "--user", f"{host_uid}:{host_gid}",
            # Writable home as tmpfs (container user needs a writable home)
            "--tmpfs", "/home/agent:rw,exec,size=512m",
            # Resource limits
            "--memory", "4g",
            "--cpus", "2",
            "--pids-limit", "256",
            # Security hardening
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            # Mount workspace as /repo
            "-v", f"{validated_path}:/repo:rw",
            "-w", "/repo",
            # Pass API keys for LLM access
            "-e", f"ZAI_API_KEY={os.environ.get('ZAI_API_KEY', '')}",
            "-e", f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
            "-e", f"OPENAI_API_KEY={os.environ.get('OPENAI_API_KEY', '')}",
            "-e", f"GEMINI_API_KEY={os.environ.get('GEMINI_API_KEY', '')}",
            "-e", "HOME=/home/agent",
        ]

        if env_vars:
            for key, value in env_vars.items():
                cmd.extend(["-e", f"{key}={value}"])

        cmd.extend([
            # Image and command
            SANDBOX_IMAGE,
            "-b", backend,
            "--model", model_name,
            "--json",
            "-",  # Read prompt from stdin
        ])
        # fmt: on

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=task.encode()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill container on timeout
                logger.warning(f"Container {container_name} timed out after {timeout}s")
                await self._kill_container(container_name)
                finished_at = datetime.now(timezone.utc)
                duration_ms = int((finished_at - started_at).total_seconds() * 1000)

                return CloudExecutionResult(
                    status="timeout",
                    output="",
                    error=f"Container execution timed out after {timeout} seconds",
                    exit_code=-1,
                    duration_ms=duration_ms,
                    model=f"{backend}/{model_name}",
                    started_at=started_at,
                    finished_at=finished_at,
                )
            except asyncio.CancelledError:
                # Kill container on cancellation
                logger.warning(f"Container {container_name} cancelled, killing")
                await self._kill_container(container_name)
                raise

            finished_at = datetime.now(timezone.utc)
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            error_output = stderr.decode("utf-8", errors="replace") if stderr else ""

            if proc.returncode == 0:
                logger.info(f"Container {container_name} completed successfully in {duration_ms}ms")
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
                logger.warning(f"Container {container_name} failed with exit code {proc.returncode}")
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
            logger.error("docker not found")
            return CloudExecutionResult(
                status="failed",
                output="",
                error="docker executable not found",
                exit_code=-1,
                model=f"{backend}/{model_name}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.exception(f"Container execution failed: {e}")
            return CloudExecutionResult(
                status="failed",
                output="",
                error=str(e),
                exit_code=-1,
                model=f"{backend}/{model_name}",
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )

    async def _kill_container(self, container_name: str) -> None:
        """Kill a Docker container by name.

        Best-effort operation - container may have already exited.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception as e:
            logger.debug(f"Container kill failed (may have already exited): {e}")

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

    async def check_sandbox_available(self) -> tuple[bool, str]:
        """Check if Docker and sandbox image are available.

        Returns
        -------
        tuple[bool, str]
            (available, message) - whether sandbox execution is available
        """
        try:
            # Check docker is available
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return False, "docker is not available"

            # Check image exists
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "image",
                "inspect",
                SANDBOX_IMAGE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode == 0:
                return True, f"sandbox image {SANDBOX_IMAGE} is available"
            else:
                return False, f"sandbox image {SANDBOX_IMAGE} not found"

        except FileNotFoundError:
            return False, "docker executable not found"
        except asyncio.TimeoutError:
            return False, "docker check timed out"
        except Exception as e:
            return False, f"Error checking sandbox: {e}"


__all__ = ["CloudExecutor", "CloudExecutionResult", "validate_workspace_path"]
