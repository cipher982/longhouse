"""Backend configurations for different AI agent CLIs.

Each backend knows how to configure environment variables and build commands
for its respective CLI tool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any

from zerg.libs.agent_runner.context import ExecutionContext
from zerg.libs.agent_runner.context import detect_context


class Backend(str, Enum):
    """Supported agent backends."""

    ZAI = "zai"  # Claude Code CLI with z.ai/GLM-4.7
    BEDROCK = "bedrock"  # Claude Code CLI with AWS Bedrock
    CODEX = "codex"  # OpenAI Codex CLI
    GEMINI = "gemini"  # Google Gemini CLI


@dataclass
class BackendConfig:
    """Configuration produced by a backend."""

    cmd: list[str]
    env: dict[str, str]
    env_unset: list[str] = field(default_factory=list)
    stdin_data: bytes | None = None  # Prompt via stdin to avoid ARG_MAX limits

    def build_env(self) -> dict[str, str]:
        """Build final environment dict."""
        # Start with current environment
        result = dict(os.environ)

        # Remove vars that need to be unset
        for key in self.env_unset:
            result.pop(key, None)

        # Apply backend-specific vars
        result.update(self.env)

        return result


def configure_zai(
    prompt: str,
    ctx: ExecutionContext | None = None,
    *,
    api_key: str | None = None,
    base_url: str = "https://api.z.ai/api/anthropic",
    model: str = "glm-4.7",
    **_: Any,
) -> BackendConfig:
    """Configure z.ai backend (Claude Code CLI with GLM-4.7).

    Key insight: z.ai uses ANTHROPIC_AUTH_TOKEN (not ANTHROPIC_API_KEY),
    and requires CLAUDE_CODE_USE_BEDROCK to be unset.

    Prompt passed via stdin to avoid ARG_MAX limits on large prompts.
    """
    ctx = ctx or detect_context()

    key = api_key or os.environ.get("ZAI_API_KEY")
    if not key:
        raise ValueError("ZAI_API_KEY not set and no api_key provided")

    env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": key,  # NOT ANTHROPIC_API_KEY
        "ANTHROPIC_MODEL": model,
    }

    # Set HOME for containers with read-only filesystems
    if ctx.in_container and not ctx.home_writable:
        env["HOME"] = "/tmp"

    # Use stdin for prompt to avoid ARG_MAX (--print reads from stdin with -)
    cmd = [
        "claude",
        "--print",
        "-",  # Read prompt from stdin
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]

    return BackendConfig(
        cmd=cmd,
        env=env,
        env_unset=["CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY"],
        stdin_data=prompt.encode("utf-8"),
    )


def configure_bedrock(
    prompt: str,
    ctx: ExecutionContext | None = None,
    *,
    model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    aws_profile: str = "zh-qa-engineer",
    aws_region: str = "us-east-1",
    **_: Any,
) -> BackendConfig:
    """Configure Bedrock backend (Claude Code CLI with AWS Bedrock).

    Prompt passed via stdin to avoid ARG_MAX limits on large prompts.
    """
    ctx = ctx or detect_context()

    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_PROFILE": aws_profile,
        "AWS_REGION": aws_region,
        "ANTHROPIC_MODEL": model,
    }

    # Set HOME for containers with read-only filesystems
    if ctx.in_container and not ctx.home_writable:
        env["HOME"] = "/tmp"

    # Use stdin for prompt to avoid ARG_MAX (--print reads from stdin with -)
    cmd = [
        "claude",
        "--print",
        "-",  # Read prompt from stdin
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]

    return BackendConfig(cmd=cmd, env=env, stdin_data=prompt.encode("utf-8"))


def configure_codex(
    prompt: str,
    ctx: ExecutionContext | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    full_auto: bool = True,
    **_: Any,
) -> BackendConfig:
    """Configure Codex backend (OpenAI Codex CLI).

    Uses `codex exec` subcommand for non-interactive mode.
    Prompt passed via stdin (using `-` as prompt arg) to avoid ARG_MAX limits.
    """
    ctx = ctx or detect_context()

    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY not set and no api_key provided")

    env = {
        "OPENAI_API_KEY": key,
    }

    # Set HOME for containers (Codex writes to ~/.codex)
    if ctx.in_container and not ctx.home_writable:
        env["HOME"] = ctx.effective_home

    # Codex exec subcommand for non-interactive mode
    # `-` means read prompt from stdin
    cmd = [
        "codex",
        "exec",
        "-",  # Read prompt from stdin
    ]

    # Full auto mode for automatic execution without prompts
    if full_auto:
        cmd.append("--full-auto")

    # Model override if specified
    if model:
        cmd.extend(["-m", model])

    return BackendConfig(cmd=cmd, env=env, stdin_data=prompt.encode("utf-8"))


def configure_gemini(
    prompt: str,
    ctx: ExecutionContext | None = None,
    **_: Any,
) -> BackendConfig:
    """Configure Gemini backend (Google Gemini CLI).

    Uses OAuth - no API key needed.
    Prompt passed via stdin to avoid ARG_MAX limits on large prompts.
    """
    ctx = ctx or detect_context()

    env: dict[str, str] = {}

    # Set HOME for containers (Gemini writes to ~/.config)
    if ctx.in_container and not ctx.home_writable:
        env["HOME"] = ctx.effective_home

    # Gemini CLI reads from stdin when given -p -
    cmd = [
        "gemini",
        "-p",
        "-",  # Read prompt from stdin
    ]

    return BackendConfig(cmd=cmd, env=env, stdin_data=prompt.encode("utf-8"))


# Backend to configure function mapping
BACKEND_CONFIGURATORS = {
    Backend.ZAI: configure_zai,
    Backend.BEDROCK: configure_bedrock,
    Backend.CODEX: configure_codex,
    Backend.GEMINI: configure_gemini,
}


def get_config(
    backend: Backend,
    prompt: str,
    ctx: ExecutionContext | None = None,
    **kwargs: Any,
) -> BackendConfig:
    """Get configuration for a backend."""
    configurator = BACKEND_CONFIGURATORS[backend]
    return configurator(prompt, ctx, **kwargs)
