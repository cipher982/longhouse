"""Legacy concierge prompt function.

DEPRECATED: Use build_concierge_prompt(user) from composer.py instead.

This module exists for backward compatibility with code that calls get_concierge_prompt()
without a user context. It returns the base template with default placeholders.
"""

from zerg.prompts.templates import BASE_CONCIERGE_PROMPT


def get_concierge_prompt() -> str:
    """Return the concierge fiche system prompt with default context.

    DEPRECATED: Use build_concierge_prompt(user) from composer.py instead.
    This function returns the base template with placeholder defaults.

    Returns:
        str: System prompt for concierge fiches
    """
    return BASE_CONCIERGE_PROMPT.format(
        user_context="(No user context configured - using defaults)",
        servers="(No servers configured)",
        integrations="(No integrations configured)",
    )
