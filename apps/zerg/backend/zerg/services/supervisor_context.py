"""Context variables for supervisor run correlation.

This module provides a thread-safe way to pass the supervisor run_id
to worker spawning during agent execution using Python's contextvars.

The pattern mirrors the credential context (connectors/context.py) where
the SupervisorService sets the context before invocation and spawn_worker reads from it.

Usage in SupervisorService.run_supervisor:
    from zerg.services.supervisor_context import set_supervisor_run_id
    token = set_supervisor_run_id(run.id)
    # ... invoke agent ...
    reset_supervisor_run_id(token)  # cleanup

Usage in spawn_worker:
    from zerg.services.supervisor_context import get_supervisor_run_id
    supervisor_run_id = get_supervisor_run_id()  # May be None
"""

from __future__ import annotations

import contextvars
from typing import Optional

# Context variable holding the current supervisor run ID
# Set by SupervisorService before invoking the agent, read by spawn_worker
_supervisor_run_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "_supervisor_run_id_var",
    default=None,
)


def get_supervisor_run_id() -> Optional[int]:
    """Get the current supervisor run ID from context.

    Returns:
        int if set (we're inside a supervisor run), None otherwise.
        spawn_worker uses this to correlate workers with the supervisor run.
    """
    return _supervisor_run_id_var.get()


def set_supervisor_run_id(run_id: Optional[int]) -> contextvars.Token:
    """Set the supervisor run ID for the current context.

    Should be called by SupervisorService before invoking the agent.
    Returns a token that can be used to reset the context.

    Args:
        run_id: The supervisor AgentRun ID, or None to clear

    Returns:
        Token for resetting the context via reset_supervisor_run_id()
    """
    return _supervisor_run_id_var.set(run_id)


def reset_supervisor_run_id(token: contextvars.Token) -> None:
    """Reset the supervisor run ID to its previous value.

    Args:
        token: Token returned by set_supervisor_run_id()
    """
    _supervisor_run_id_var.reset(token)


__all__ = [
    "get_supervisor_run_id",
    "set_supervisor_run_id",
    "reset_supervisor_run_id",
]
