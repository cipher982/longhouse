"""Legacy worker prompt function.

DEPRECATED: Use build_worker_prompt(user) from composer.py instead.

This module exists for backward compatibility with code that calls get_worker_prompt()
without a user context. It returns the base template with default placeholders.
"""

from zerg.prompts.templates import BASE_WORKER_PROMPT


def get_worker_prompt() -> str:
    """Return the worker agent system prompt with default context.

    DEPRECATED: Use build_worker_prompt(user) from composer.py instead.
    This function returns the base template with placeholder defaults.

    Returns:
        str: System prompt for worker agents
    """
    return BASE_WORKER_PROMPT.format(
        servers="(No servers configured)",
        user_context="(No user context configured - using defaults)",
    )
