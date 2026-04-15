"""Prompt components for fiche system instructions."""

from zerg.prompts.composer import build_commis_prompt
from zerg.prompts.composer import build_oikos_prompt
from zerg.prompts.composer import format_integrations
from zerg.prompts.composer import format_server_names
from zerg.prompts.composer import format_servers
from zerg.prompts.composer import format_user_context

__all__ = [
    "build_oikos_prompt",
    "build_commis_prompt",
    "format_user_context",
    "format_servers",
    "format_server_names",
    "format_integrations",
]
