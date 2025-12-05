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

Sequence Counter:
    Each supervisor run has a monotonically increasing sequence counter for SSE events.
    This enables idempotent reconnect handling - clients can dedupe events via (run_id, seq).
"""

from __future__ import annotations

import contextvars
import threading
from typing import Dict, Optional

# Context variable holding the current supervisor run ID
# Set by SupervisorService before invoking the agent, read by spawn_worker
_supervisor_run_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "_supervisor_run_id_var",
    default=None,
)

# Sequence counters per run_id - thread-safe dict with lock
_sequence_counters: Dict[int, int] = {}
_sequence_lock = threading.Lock()


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


def get_next_seq(run_id: int) -> int:
    """Get the next sequence number for a supervisor run.

    Thread-safe, monotonically increasing counter per run_id.
    Used by SSE events for idempotent reconnect handling.

    Args:
        run_id: The supervisor run ID

    Returns:
        Next sequence number (starts at 1, increments each call)
    """
    with _sequence_lock:
        current = _sequence_counters.get(run_id, 0)
        next_seq = current + 1
        _sequence_counters[run_id] = next_seq
        return next_seq


def reset_seq(run_id: int) -> None:
    """Reset the sequence counter for a run.

    Called when a run completes to clean up memory.

    Args:
        run_id: The supervisor run ID to clean up
    """
    with _sequence_lock:
        _sequence_counters.pop(run_id, None)


__all__ = [
    "get_supervisor_run_id",
    "set_supervisor_run_id",
    "reset_supervisor_run_id",
    "get_next_seq",
    "reset_seq",
]
