"""Prompt components for agent system instructions."""

# Legacy prompt functions (deprecated - use composer functions instead)
from zerg.prompts.supervisor_prompt import get_supervisor_prompt
from zerg.prompts.worker_prompt import get_worker_prompt

# New context-aware prompt composition
from zerg.prompts.composer import (
    build_supervisor_prompt,
    build_worker_prompt,
    build_jarvis_prompt,
    format_user_context,
    format_servers,
    format_server_names,
    format_integrations,
)

__all__ = [
    # Legacy (deprecated)
    "get_supervisor_prompt",
    "get_worker_prompt",
    # New context-aware builders
    "build_supervisor_prompt",
    "build_worker_prompt",
    "build_jarvis_prompt",
    # Formatters (for testing/debugging)
    "format_user_context",
    "format_servers",
    "format_server_names",
    "format_integrations",
]
