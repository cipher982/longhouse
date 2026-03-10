"""Context variables for oikos run correlation.

Thread-safe contextvars passing run_id/owner_id/message_id to commis spawning
during fiche execution. Sequence counter per run enables idempotent SSE reconnect.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Dict
from typing import Optional


@dataclass
class OikosContext:
    """Context data for oikos run correlation and event emission."""

    run_id: int
    owner_id: int
    message_id: str  # UUID for the assistant message (stable across tokens/completion)
    trace_id: Optional[str] = None  # End-to-end trace ID for debugging (UUID as string)
    model: Optional[str] = None  # Model ID for commis to inherit
    reasoning_effort: Optional[str] = None  # Reasoning effort for commis to inherit


# Context variable holding the current oikos context
# Set by OikosService before invoking the fiche
_oikos_context_var: contextvars.ContextVar[Optional[OikosContext]] = contextvars.ContextVar(
    "_oikos_context_var",
    default=None,
)

# Sequence counters per run_id - thread-safe dict with lock
_sequence_counters: Dict[int, int] = {}
_sequence_lock = threading.Lock()


def get_oikos_context() -> Optional[OikosContext]:
    """Get the current oikos context (None if not inside a run)."""
    return _oikos_context_var.get()


def set_oikos_context(
    run_id: int,
    owner_id: int,
    message_id: str,
    trace_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> contextvars.Token:
    """Set the oikos context before invoking a fiche. Returns token for reset."""
    ctx = OikosContext(
        run_id=run_id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return _oikos_context_var.set(ctx)


def reset_oikos_context(token: contextvars.Token) -> None:
    """Reset the oikos context to its previous value."""
    _oikos_context_var.reset(token)


def reset_seq(run_id: int) -> None:
    """Reset the sequence counter for a completed run (memory cleanup)."""
    with _sequence_lock:
        _sequence_counters.pop(run_id, None)


__all__ = [
    "OikosContext",
    "get_oikos_context",
    "set_oikos_context",
    "reset_oikos_context",
    "reset_seq",
]
