"""Context variables for supervisor run correlation.

This module provides a thread-safe way to pass the supervisor run_id
to worker spawning during agent execution using Python's contextvars.

The pattern mirrors the credential context (connectors/context.py) where
the SupervisorService sets the context before invocation and spawn_worker reads from it.

Usage in SupervisorService.run_supervisor:
    from zerg.services.supervisor_context import set_supervisor_context
    token = set_supervisor_context(run_id=run.id, owner_id=owner_id, message_id=msg_id)
    # ... invoke agent ...
    reset_supervisor_context(token)  # cleanup

Usage in spawn_worker / tool event emission:
    from zerg.services.supervisor_context import get_supervisor_context
    ctx = get_supervisor_context()  # Returns SupervisorContext or None
    if ctx:
        run_id, owner_id = ctx.run_id, ctx.owner_id

Sequence Counter:
    Each supervisor run has a monotonically increasing sequence counter for SSE events.
    This enables idempotent reconnect handling - clients can dedupe events via (run_id, seq).
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Dict
from typing import Optional


@dataclass
class SupervisorContext:
    """Context data for supervisor run correlation and event emission."""

    run_id: int
    owner_id: int
    message_id: str  # UUID for the assistant message (stable across tokens/completion)
    trace_id: Optional[str] = None  # End-to-end trace ID for debugging (UUID as string)
    model: Optional[str] = None  # Model ID for workers to inherit
    reasoning_effort: Optional[str] = None  # Reasoning effort for workers to inherit


# Context variable holding the current supervisor context
# Set by SupervisorService before invoking the agent
_supervisor_context_var: contextvars.ContextVar[Optional[SupervisorContext]] = contextvars.ContextVar(
    "_supervisor_context_var",
    default=None,
)

# Sequence counters per run_id - thread-safe dict with lock
_sequence_counters: Dict[int, int] = {}
_sequence_lock = threading.Lock()


def get_supervisor_context() -> Optional[SupervisorContext]:
    """Get the current supervisor context.

    Returns:
        SupervisorContext if set (we're inside a supervisor run), None otherwise.
        Contains run_id, owner_id, and message_id for event correlation.
    """
    return _supervisor_context_var.get()


def set_supervisor_context(
    run_id: int,
    owner_id: int,
    message_id: str,
    trace_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> contextvars.Token:
    """Set the supervisor context for the current execution.

    Should be called by SupervisorService before invoking the agent.
    Returns a token that can be used to reset the context.

    Args:
        run_id: The supervisor AgentRun ID
        owner_id: The owner's user ID
        message_id: UUID for the assistant message
        trace_id: End-to-end trace ID for debugging (optional)
        model: Model ID for workers to inherit (optional)
        reasoning_effort: Reasoning effort for workers to inherit (optional)

    Returns:
        Token for resetting via reset_supervisor_context()
    """
    ctx = SupervisorContext(
        run_id=run_id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return _supervisor_context_var.set(ctx)


def reset_supervisor_context(token: contextvars.Token) -> None:
    """Reset the supervisor context to its previous value.

    Args:
        token: Token returned by set_supervisor_context()
    """
    _supervisor_context_var.reset(token)


def get_supervisor_message_id() -> Optional[str]:
    """Get the current supervisor message_id from context.

    Returns:
        str if set (we're inside a supervisor run), None otherwise.
        Used for including message_id in SSE events.
    """
    ctx = _supervisor_context_var.get()
    return ctx.message_id if ctx else None


def get_supervisor_trace_id() -> Optional[str]:
    """Get the current supervisor trace_id from context.

    Returns:
        str if set (we're inside a supervisor run with trace_id), None otherwise.
        Used for end-to-end debugging across supervisor/worker boundaries.
    """
    ctx = _supervisor_context_var.get()
    return ctx.trace_id if ctx else None


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
    "SupervisorContext",
    "get_supervisor_context",
    "set_supervisor_context",
    "reset_supervisor_context",
    "get_supervisor_message_id",
    "get_supervisor_trace_id",
    "get_next_seq",
    "reset_seq",
]
