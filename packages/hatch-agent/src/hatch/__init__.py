"""Unified agent runner library.

Simple subprocess wrapper for running Claude/Codex/Gemini headlessly.
Handles env var configuration for each backend and container/laptop detection.

Usage:
    from hatch import run, Backend

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

from hatch.backends import Backend
from hatch.runner import AgentResult
from hatch.runner import run

__version__ = "0.1.0"
__all__ = ["run", "Backend", "AgentResult", "__version__"]
