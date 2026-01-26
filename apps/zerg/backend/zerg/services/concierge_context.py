"""Context variables for concierge course correlation.

This module provides a thread-safe way to pass the concierge course_id
to commis spawning during fiche execution using Python's contextvars.

The pattern mirrors the credential context (connectors/context.py) where
the ConciergeService sets the context before invocation and spawn_commis reads from it.

Usage in ConciergeService.run_concierge:
    from zerg.services.concierge_context import set_concierge_context
    token = set_concierge_context(course_id=course.id, owner_id=owner_id, message_id=msg_id)
    # ... invoke fiche ...
    reset_concierge_context(token)  # cleanup

Usage in spawn_commis / tool event emission:
    from zerg.services.concierge_context import get_concierge_context
    ctx = get_concierge_context()  # Returns ConciergeContext or None
    if ctx:
        course_id, owner_id = ctx.course_id, ctx.owner_id

Sequence Counter:
    Each concierge course has a monotonically increasing sequence counter for SSE events.
    This enables idempotent reconnect handling - clients can dedupe events via (course_id, seq).
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Dict
from typing import Optional


@dataclass
class ConciergeContext:
    """Context data for concierge course correlation and event emission."""

    course_id: int
    owner_id: int
    message_id: str  # UUID for the assistant message (stable across tokens/completion)
    trace_id: Optional[str] = None  # End-to-end trace ID for debugging (UUID as string)
    model: Optional[str] = None  # Model ID for commis to inherit
    reasoning_effort: Optional[str] = None  # Reasoning effort for commis to inherit


# Context variable holding the current concierge context
# Set by ConciergeService before invoking the fiche
_concierge_context_var: contextvars.ContextVar[Optional[ConciergeContext]] = contextvars.ContextVar(
    "_concierge_context_var",
    default=None,
)

# Sequence counters per course_id - thread-safe dict with lock
_sequence_counters: Dict[int, int] = {}
_sequence_lock = threading.Lock()


def get_concierge_context() -> Optional[ConciergeContext]:
    """Get the current concierge context.

    Returns:
        ConciergeContext if set (we're inside a concierge course), None otherwise.
        Contains course_id, owner_id, and message_id for event correlation.
    """
    return _concierge_context_var.get()


def set_concierge_context(
    course_id: int,
    owner_id: int,
    message_id: str,
    trace_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> contextvars.Token:
    """Set the concierge context for the current execution.

    Should be called by ConciergeService before invoking the fiche.
    Returns a token that can be used to reset the context.

    Args:
        course_id: The concierge Course ID
        owner_id: The owner's user ID
        message_id: UUID for the assistant message
        trace_id: End-to-end trace ID for debugging (optional)
        model: Model ID for commis to inherit (optional)
        reasoning_effort: Reasoning effort for commis to inherit (optional)

    Returns:
        Token for resetting via reset_concierge_context()
    """
    ctx = ConciergeContext(
        course_id=course_id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return _concierge_context_var.set(ctx)


def reset_concierge_context(token: contextvars.Token) -> None:
    """Reset the concierge context to its previous value.

    Args:
        token: Token returned by set_concierge_context()
    """
    _concierge_context_var.reset(token)


def get_concierge_message_id() -> Optional[str]:
    """Get the current concierge message_id from context.

    Returns:
        str if set (we're inside a concierge course), None otherwise.
        Used for including message_id in SSE events.
    """
    ctx = _concierge_context_var.get()
    return ctx.message_id if ctx else None


def get_concierge_trace_id() -> Optional[str]:
    """Get the current concierge trace_id from context.

    Returns:
        str if set (we're inside a concierge course with trace_id), None otherwise.
        Used for end-to-end debugging across concierge/commis boundaries.
    """
    ctx = _concierge_context_var.get()
    return ctx.trace_id if ctx else None


def get_next_seq(course_id: int) -> int:
    """Get the next sequence number for a concierge course.

    Thread-safe, monotonically increasing counter per course_id.
    Used by SSE events for idempotent reconnect handling.

    Args:
        course_id: The concierge course ID

    Returns:
        Next sequence number (starts at 1, increments each call)
    """
    with _sequence_lock:
        current = _sequence_counters.get(course_id, 0)
        next_seq = current + 1
        _sequence_counters[course_id] = next_seq
        return next_seq


def reset_seq(course_id: int) -> None:
    """Reset the sequence counter for a course.

    Called when a course completes to clean up memory.

    Args:
        course_id: The concierge course ID to clean up
    """
    with _sequence_lock:
        _sequence_counters.pop(course_id, None)


__all__ = [
    "ConciergeContext",
    "get_concierge_context",
    "set_concierge_context",
    "reset_concierge_context",
    "get_concierge_message_id",
    "get_concierge_trace_id",
    "get_next_seq",
    "reset_seq",
]
