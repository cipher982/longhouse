"""Unified agent runner library.

Simple subprocess wrapper for running Claude/Codex/Gemini headlessly.
Handles env var configuration for each backend and container/laptop detection.

Usage:
    from zerg.libs.agent_runner import run, Backend

    result = await run(
        prompt="Fix the bug",
        backend=Backend.ZAI,
        cwd="/path/to/workspace",
        timeout_s=300,
    )
    if result.ok:
        print(result.output)
    else:
        print(f"Failed: {result.error}")
"""

from zerg.libs.agent_runner.backends import Backend
from zerg.libs.agent_runner.runner import AgentResult
from zerg.libs.agent_runner.runner import run

__all__ = ["run", "Backend", "AgentResult"]
