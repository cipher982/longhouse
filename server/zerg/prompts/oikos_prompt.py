"""Legacy oikos prompt function.

DEPRECATED: Use build_oikos_prompt(user) from composer.py instead.

This module exists for backward compatibility with code that calls get_oikos_prompt()
without a user context. It returns the base template with default placeholders.
"""

from zerg.prompts.templates import BASE_OIKOS_PROMPT


def get_oikos_prompt() -> str:
    """Return the oikos fiche system prompt with default context.

    DEPRECATED: Use build_oikos_prompt(user) from composer.py instead.
    This function returns the base template with placeholder defaults.

    Returns:
        str: System prompt for oikos fiches
    """
    return BASE_OIKOS_PROMPT.format(
        user_context="(No user context configured - using defaults)",
        servers="(No servers configured)",
        integrations="(No integrations configured)",
    )
