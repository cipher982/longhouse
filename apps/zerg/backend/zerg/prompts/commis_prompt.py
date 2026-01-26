"""Legacy commis prompt function.

DEPRECATED: Use build_commis_prompt(user) from composer.py instead.

This module exists for backward compatibility with code that calls get_commis_prompt()
without a user context. It returns the base template with default placeholders.
"""

from zerg.prompts.templates import BASE_COMMIS_PROMPT


def get_commis_prompt() -> str:
    """Return the commis system prompt with default context.

    DEPRECATED: Use build_commis_prompt(user) from composer.py instead.
    This function returns the base template with placeholder defaults.

    Returns:
        str: System prompt for commis
    """
    return BASE_COMMIS_PROMPT.format(
        servers="(No servers configured)",
        user_context="(No user context configured - using defaults)",
        online_runners="(No runners configured)",
    )
