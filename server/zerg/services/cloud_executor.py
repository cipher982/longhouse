"""Cloud Executor – run headless commis execution via subprocess.

This service runs commis as headless subprocesses using the `hatch` CLI tool.
It enables 24/7 cloud execution independent of laptop connectivity.

Workspace isolation is handled by WorkspaceManager (directory-based isolation
with git clones). Each commis gets its own working directory and git branch.

Usage:
    executor = CloudExecutor()
    result = await executor.run_commis(
        task="Fix the typo in README.md",
        workspace_path="~/.longhouse/workspaces/run-123",
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

# Default timeout for commis execution (1 hour)
DEFAULT_TIMEOUT_SECONDS = 3600

# Legacy default model retained for optional explicit use.
# By default we now pass no backend/model flags and let hatch choose defaults.
DEFAULT_CLOUD_MODEL = "zai/glm-4.7"

CLAUDE_BACKENDS = {"zai", "bedrock", "anthropic"}
KNOWN_BACKENDS = {"zai", "codex", "gemini", "bedrock", "anthropic"}
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
    # Test-only models (used in E2E)
    "gpt-scripted": "zai/gpt-scripted",
    "gpt-mock": "zai/gpt-mock",
}


def _coerce_bool(value: str | bool | None) -> bool:
    """Coerce common string/bool values to bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _infer_backend_for_model(model: str) -> str:
    """Infer hatch backend from an unqualified model ID."""
    lowered = model.lower()
    if lowered.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return "codex"
    if lowered.startswith("claude-") or lowered.startswith("us.anthropic."):
        return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "bedrock"
    if lowered.startswith("glm-"):
        return "zai"
    if lowered.startswith("gemini-"):
        return "gemini"
    raise ValueError(
        f"Unknown model '{model}'. Provide backend explicitly (e.g. backend='zai') "
        "or use a recognized model prefix (gpt-/o1-/o3-/o4-/claude-/us.anthropic./glm-/gemini-)."
    )


def normalize_model_id(model: str) -> tuple[str, str]:
    """Normalize a model-only override into (backend, model_name)."""
    if "/" in model:
        backend, model_name = model.split("/", 1)
        return backend, model_name

    mapped = MODEL_MAPPING.get(model)
    if mapped:
        backend, model_name = mapped.split("/", 1)
        return backend, model_name

    return _infer_backend_for_model(model), model


def resolve_backend_and_model(model: str | None, backend: str | None) -> tuple[str | None, str | None]:
    """Resolve optional backend/model inputs into hatch CLI flags.

    Rules:
    - backend + model -> pass both
    - backend only -> pass backend only
    - model only -> infer backend via compat mapping/prefixes
    - neither -> pass neither (hatch defaults)
    """
    normalized_backend = backend.strip() if backend and backend.strip() else None
    normalized_model = model.strip() if model and model.strip() else None

    if normalized_backend and normalized_backend not in KNOWN_BACKENDS:
        raise ValueError(f"Unknown backend '{normalized_backend}'. Supported backends: {sorted(KNOWN_BACKENDS)}")

    if normalized_backend and normalized_model:
        if "/" in normalized_model:
            embedded_backend, embedded_model = normalized_model.split("/", 1)
            if embedded_backend != normalized_backend:
                raise ValueError("Conflicting backend inputs: " f"backend='{normalized_backend}' but model='{normalized_model}'")
            normalized_model = embedded_model
        return normalized_backend, normalized_model

    if normalized_backend:
        return normalized_backend, None

    if normalized_model:
        return normalize_model_id(normalized_model)

    return None, None


def _format_selected_model(backend: str | None, model: str | None) -> str:
    """Format selected execution target for logs/results."""
    if backend and model:
        return f"{backend}/{model}"
    if backend:
        return f"{backend}/(default)"
    if model:
        return model
    return "(hatch-default)"


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
        default_model: str | None = None,
        claude_output_format: str | None = None,
        include_partial_messages: bool | None = None,
    ):
        """Initialize the cloud executor.

        Parameters
        ----------
        hatch_path
            Path to hatch executable. If None, uses 'hatch' from PATH.
        default_model
            Optional legacy model default to use when run_commis() does not
            pass model/backend overrides. Leave unset to use hatch defaults.
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
        backend: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        env_vars: dict[str, str] | None = None,
        resume_session_id: str | None = None,
    ) -> CloudExecutionResult:
        """Execute a task using hatch in the given workspace.

        This method:
        1. Spawns hatch subprocess in the workspace
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
            Optional model override. If backend is omitted, backend is inferred.
        backend
            Optional backend override (zai/codex/gemini/bedrock/anthropic).
            If provided without model, hatch backend defaults are used.
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
        # Resolve backend/model selection.
        # If both are omitted, we pass no flags and let hatch defaults apply.
        raw_model = model if model is not None else self.default_model
        requested_target = _format_selected_model(backend, raw_model)
        try:
            resolved_backend, resolved_model = resolve_backend_and_model(raw_model, backend)
        except ValueError as exc:
            return CloudExecutionResult(
                status="failed",
                output="",
                error=str(exc),
                exit_code=-1,
                model=requested_target,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

        selected_target = _format_selected_model(resolved_backend, resolved_model)
        backend_for_options = resolved_backend or "zai"
        started_at = datetime.now(timezone.utc)

        logger.info(
            "Starting cloud execution in %s with backend=%s model=%s",
            workspace,
            resolved_backend or "(hatch-default)",
            resolved_model or "(hatch-default)",
        )
        logger.debug(f"Task: {task[:200]}...")

        # Build command for hatch CLI
        # hatch [-b <backend>] [--model <model>] -C <workspace> [--resume <id>] "<prompt>"
        cmd = [self.hatch_path]
        if resolved_backend:
            cmd.extend(["-b", resolved_backend])
        if resolved_model:
            cmd.extend(["--model", resolved_model])
        cmd.extend(["-C", str(workspace)])

        if self.claude_output_format and backend_for_options in CLAUDE_BACKENDS:
            cmd.extend(["--output-format", self.claude_output_format])

        if self.include_partial_messages and backend_for_options in CLAUDE_BACKENDS:
            cmd.append("--include-partial-messages")

        # Add resume flag for session continuity (Claude backends only)
        if resume_session_id and backend_for_options in CLAUDE_BACKENDS:
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
                model=selected_target,
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
                    model=selected_target,
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
                    model=selected_target,
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
                    model=selected_target,
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
                model=selected_target,
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
                model=selected_target,
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
