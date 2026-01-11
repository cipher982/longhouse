"""Context variables for supervisor run correlation.

This module provides a thread-safe way to pass the supervisor run_id
to worker spawning during agent execution using Python's contextvars.

The pattern mirrors the credential context (connectors/context.py) where
the SupervisorService sets the context before invocation and spawn_worker reads from it.

Usage in SupervisorService.run_supervisor:
    from zerg.services.supervisor_context import set_supervisor_context
    token = set_supervisor_context(run_id=run.id, db=db, owner_id=owner_id)
    # ... invoke agent ...
    reset_supervisor_context(token)  # cleanup

Usage in spawn_worker / tool event emission:
    from zerg.services.supervisor_context import get_supervisor_context
    ctx = get_supervisor_context()  # Returns SupervisorContext or None
    if ctx:
        run_id, db, owner_id = ctx.run_id, ctx.db, ctx.owner_id

Sequence Counter:
    Each supervisor run has a monotonically increasing sequence counter for SSE events.
    This enables idempotent reconnect handling - clients can dedupe events via (run_id, seq).
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Dict
from typing import Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass
class SupervisorContext:
    """Context data for supervisor run correlation and event emission."""

    run_id: int
    db: "Session"
    owner_id: int
    message_id: str  # UUID for the assistant message (stable across tokens/completion)


# Context variable holding the current supervisor context
# Set by SupervisorService before invoking the agent
_supervisor_context_var: contextvars.ContextVar[Optional[SupervisorContext]] = contextvars.ContextVar(
    "_supervisor_context_var",
    default=None,
)

# Legacy: Keep run_id only var for backwards compatibility with spawn_worker
# TODO: Migrate spawn_worker to use get_supervisor_context() and remove this
_supervisor_run_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "_supervisor_run_id_var",
    default=None,
)

# Sequence counters per run_id - thread-safe dict with lock
_sequence_counters: Dict[int, int] = {}
_sequence_lock = threading.Lock()


def get_supervisor_context() -> Optional[SupervisorContext]:
    """Get the current supervisor context.

    Returns:
        SupervisorContext if set (we're inside a supervisor run), None otherwise.
        Contains run_id, db session, and owner_id for event emission.
    """
    return _supervisor_context_var.get()


def set_supervisor_context(run_id: int, db: "Session", owner_id: int, message_id: str) -> tuple[contextvars.Token, contextvars.Token]:
    """Set the supervisor context for the current execution.

    Should be called by SupervisorService before invoking the agent.
    Returns tokens that can be used to reset the context.

    Args:
        run_id: The supervisor AgentRun ID
        db: SQLAlchemy database session
        owner_id: The owner's user ID
        message_id: UUID for the assistant message

    Returns:
        Tuple of tokens for resetting via reset_supervisor_context()
    """
    ctx = SupervisorContext(run_id=run_id, db=db, owner_id=owner_id, message_id=message_id)
    token_ctx = _supervisor_context_var.set(ctx)
    # Also set legacy run_id var for backwards compatibility
    token_run_id = _supervisor_run_id_var.set(run_id)
    return (token_ctx, token_run_id)


def reset_supervisor_context(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    """Reset the supervisor context to its previous value.

    Args:
        tokens: Tuple of tokens returned by set_supervisor_context()
    """
    token_ctx, token_run_id = tokens
    _supervisor_context_var.reset(token_ctx)
    _supervisor_run_id_var.reset(token_run_id)


def get_supervisor_message_id() -> Optional[str]:
    """Get the current supervisor message_id from context.

    Returns:
        str if set (we're inside a supervisor run), None otherwise.
        Used for including message_id in SSE events.
    """
    ctx = _supervisor_context_var.get()
    return ctx.message_id if ctx else None


# Legacy functions for backwards compatibility
def get_supervisor_run_id() -> Optional[int]:
    """Get the current supervisor run ID from context.

    Returns:
        int if set (we're inside a supervisor run), None otherwise.
        spawn_worker uses this to correlate workers with the supervisor run.

    Note: Prefer get_supervisor_context() for new code.
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

    Note: Prefer set_supervisor_context() for new code.
    """
    return _supervisor_run_id_var.set(run_id)


def reset_supervisor_run_id(token: contextvars.Token) -> None:
    """Reset the supervisor run ID to its previous value.

    Args:
        token: Token returned by set_supervisor_run_id()

    Note: Prefer reset_supervisor_context() for new code.
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
    # New API
    "SupervisorContext",
    "get_supervisor_context",
    "set_supervisor_context",
    "reset_supervisor_context",
    "get_supervisor_message_id",
    # Legacy (for spawn_worker compatibility)
    "get_supervisor_run_id",
    "set_supervisor_run_id",
    "reset_supervisor_run_id",
    # Sequence counter
    "get_next_seq",
    "reset_seq",
]
