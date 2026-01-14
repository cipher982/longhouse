"""Worker context for cross-cutting concerns.

This module provides a contextvar-based mechanism for passing worker context
through the call stack without explicit parameter threading. This is particularly
useful for emitting events from deep within the agent execution (e.g., tool calls)
without modifying function signatures.

Usage:
    # In WorkerRunner.run_worker():
    ctx = WorkerContext(worker_id="...", owner_id=1, run_id="...")
    token = set_worker_context(ctx)
    try:
        await agent.run()
    finally:
        reset_worker_context(token)

    # In supervisor_react_engine._call_tool_async():
    ctx = get_worker_context()
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
class WorkerContext:
    """Context for a running worker, accessible via contextvar.

    This context is set by WorkerRunner at the start of a worker run and
    can be accessed from anywhere in the call stack (including inside
    tool execution via asyncio.to_thread).

    Attributes
    ----------
    worker_id
        Unique identifier for the worker (e.g., "2024-12-05T16-30-00_disk-check")
    owner_id
        User ID that owns this worker's agent
    run_id
        Optional run ID for correlating events
    job_id
        Optional WorkerJob ID for roundabout event correlation
    task
        Task description (first 100 chars)
    tool_calls
        List of tool calls made during this worker run (for activity log)
    has_critical_error
        Flag indicating a critical tool error occurred (fail-fast)
    critical_error_message
        Human-readable error message for the critical error
    """

    worker_id: str
    owner_id: int | None = None
    run_id: str | None = None
    job_id: int | None = None
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


# Global contextvar - set by WorkerRunner, read anywhere in the call stack
_worker_ctx: ContextVar[WorkerContext | None] = ContextVar("worker_ctx", default=None)


def get_worker_context() -> WorkerContext | None:
    """Get the current worker context, if running inside a worker.

    Returns None if not in a worker context (e.g., supervisor or direct agent call).
    """
    return _worker_ctx.get()


def set_worker_context(ctx: WorkerContext) -> Token[WorkerContext | None]:
    """Set the worker context. Returns a token for reset.

    Must be paired with reset_worker_context() in a finally block.
    """
    return _worker_ctx.set(ctx)


def reset_worker_context(token: Token[WorkerContext | None]) -> None:
    """Reset the worker context to its previous value."""
    _worker_ctx.reset(token)


__all__ = [
    "WorkerContext",
    "ToolCall",
    "get_worker_context",
    "set_worker_context",
    "reset_worker_context",
]
