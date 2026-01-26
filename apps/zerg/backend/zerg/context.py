"""Commis context for cross-cutting concerns.

This module provides a contextvar-based mechanism for passing commis context
through the call stack without explicit parameter threading. This is particularly
useful for emitting events from deep within the fiche execution (e.g., tool calls)
without modifying function signatures.

Usage:
    # In CommisRunner.run_commis():
    ctx = CommisContext(commis_id="...", owner_id=1, course_id="...")
    token = set_commis_context(ctx)
    try:
        await fiche.run()
    finally:
        reset_commis_context(token)

    # In concierge_react_engine._call_tool_async():
    ctx = get_commis_context()
    if ctx:
        await emit_tool_started_event(ctx, tool_name, ...)
"""

from __future__ import annotations

from contextvars import ContextVar
from contextvars import Token
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import Any


@dataclass
class ToolCall:
    """Record of a tool call for activity tracking."""

    name: str
    tool_call_id: str | None = None
    args_preview: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    status: str = "running"  # running, completed, failed
    duration_ms: int | None = None
    error: str | None = None


@dataclass
class CommisContext:
    """Context for a running commis, accessible via contextvar.

    This context is set by CommisRunner at the start of a commis run and
    can be accessed from anywhere in the call stack (including inside
    tool execution via asyncio.to_thread).

    Attributes
    ----------
    commis_id
        Unique identifier for the commis (e.g., "2024-12-05T16-30-00_disk-check")
    owner_id
        User ID that owns this commis's fiche
    course_id
        Optional concierge course ID for correlating events
    job_id
        Optional CommisJob ID for roundabout event correlation
    trace_id
        Optional trace ID for end-to-end debugging (inherited from concierge)
    task
        Task description (first 100 chars)
    tool_calls
        List of tool calls made during this commis run (for activity log)
    has_critical_error
        Flag indicating a critical tool error occurred (fail-fast)
    critical_error_message
        Human-readable error message for the critical error
    """

    commis_id: str
    owner_id: int | None = None
    course_id: int | None = None
    job_id: int | None = None
    trace_id: str | None = None
    task: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    has_critical_error: bool = False
    critical_error_message: str | None = None

    def record_tool_start(
        self,
        tool_name: str,
        tool_call_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> ToolCall:
        """Record a tool call starting. Returns the ToolCall for later update."""
        args_preview = str(args)[:100] if args else ""
        tool_call = ToolCall(
            name=tool_name,
            tool_call_id=tool_call_id,
            args_preview=args_preview,
        )
        self.tool_calls.append(tool_call)
        return tool_call

    def record_tool_complete(
        self,
        tool_call: ToolCall,
        *,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        """Record a tool call completing."""
        tool_call.completed_at = datetime.now(timezone.utc)
        tool_call.status = "completed" if success else "failed"
        tool_call.error = error
        if tool_call.started_at:
            delta = tool_call.completed_at - tool_call.started_at
            tool_call.duration_ms = int(delta.total_seconds() * 1000)

    def mark_critical_error(self, error_message: str) -> None:
        """Mark that a critical error occurred, triggering fail-fast behavior."""
        self.has_critical_error = True
        self.critical_error_message = error_message


# Global contextvar - set by CommisRunner, read anywhere in the call stack
_commis_ctx: ContextVar[CommisContext | None] = ContextVar("commis_ctx", default=None)


def get_commis_context() -> CommisContext | None:
    """Get the current commis context, if running inside a commis.

    Returns None if not in a commis context (e.g., concierge or direct fiche call).
    """
    return _commis_ctx.get()


def set_commis_context(ctx: CommisContext) -> Token[CommisContext | None]:
    """Set the commis context. Returns a token for reset.

    Must be paired with reset_commis_context() in a finally block.
    """
    return _commis_ctx.set(ctx)


def reset_commis_context(token: Token[CommisContext | None]) -> None:
    """Reset the commis context to its previous value."""
    _commis_ctx.reset(token)


__all__ = [
    "CommisContext",
    "ToolCall",
    "get_commis_context",
    "set_commis_context",
    "reset_commis_context",
]
