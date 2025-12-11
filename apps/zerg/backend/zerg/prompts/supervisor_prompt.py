"""Legacy supervisor prompt function.

DEPRECATED: Use build_supervisor_prompt(user) from composer.py instead.

This module exists for backward compatibility with code that calls get_supervisor_prompt()
without a user context. It returns the base template with default placeholders.
"""

from zerg.prompts.templates import BASE_SUPERVISOR_PROMPT


def get_supervisor_prompt() -> str:
    """Return the supervisor agent system prompt with default context.

    DEPRECATED: Use build_supervisor_prompt(user) from composer.py instead.
    This function returns the base template with placeholder defaults.

    Returns:
        str: System prompt for supervisor agents
    """
    return BASE_SUPERVISOR_PROMPT.format(
        user_context="(No user context configured - using defaults)",
        servers="(No servers configured)",
        integrations="(No integrations configured)",
    )
