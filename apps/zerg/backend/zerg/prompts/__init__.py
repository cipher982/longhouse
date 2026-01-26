"""Prompt components for fiche system instructions."""

# Legacy prompt functions (deprecated - use composer functions instead)
from zerg.prompts.commis_prompt import get_commis_prompt
from zerg.prompts.composer import build_commis_prompt

# New context-aware prompt composition
from zerg.prompts.composer import build_concierge_prompt
from zerg.prompts.composer import build_jarvis_prompt
from zerg.prompts.composer import format_integrations
from zerg.prompts.composer import format_server_names
from zerg.prompts.composer import format_servers
from zerg.prompts.composer import format_user_context
from zerg.prompts.concierge_prompt import get_concierge_prompt

__all__ = [
    # Legacy (deprecated)
    "get_concierge_prompt",
    "get_commis_prompt",
    # New context-aware builders
    "build_concierge_prompt",
    "build_commis_prompt",
    "build_jarvis_prompt",
    # Formatters (for testing/debugging)
    "format_user_context",
    "format_servers",
    "format_server_names",
    "format_integrations",
]
