"""WorkerEmitter - emits worker_tool_* events with identity baked in.

This class replaces the contextvar-based event emission pattern. The emitter's
identity (worker) is fixed at construction time, so it always emits the correct
event type regardless of contextvar state.

Key design principle: The emitter does NOT hold a DB session. Event emission
uses append_run_event() which opens its own short-lived session. This prevents
DB sessions from crossing async/thread boundaries via contextvars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import Any

logger = logging.getLogger(__name__)


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
class WorkerEmitter:
    """Emitter for worker tool events - identity baked at construction.

    Always emits worker_tool_* events, regardless of contextvar state.
    This eliminates the contextvar leakage bug where supervisor events
    could be misclassified as worker events.

    Key principle: No DB session stored. Event emission uses append_run_event()
    which opens its own short-lived session per event.

    Attributes
    ----------
    worker_id
        Unique identifier for the worker (e.g., "2024-12-05T16-30-00_disk-check")
    owner_id
        User ID that owns this worker's agent
    run_id
        Run ID for correlating events (supervisor run ID)
    job_id
        WorkerJob ID for roundabout event correlation (critical!)
    tool_calls
        List of tool calls made during this worker run (for activity log)
    has_critical_error
        Flag indicating a critical tool error occurred (fail-fast)
    critical_error_message
        Human-readable error message for the critical error
    """

    worker_id: str
    owner_id: int | None
    run_id: int | None
    job_id: int | None
    trace_id: str | None = None

    # Tool tracking (existing WorkerContext functionality)
    tool_calls: list[ToolCall] = field(default_factory=list)
    has_critical_error: bool = False
    critical_error_message: str | None = None

    @property
    def is_worker(self) -> bool:
        """Always True - this is a worker emitter."""
        return True

    @property
    def is_supervisor(self) -> bool:
        """Always False - this is a worker emitter."""
        return False

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

    async def emit_tool_started(
        self,
        tool_name: str,
        tool_call_id: str,
        tool_args_preview: str,
        tool_args: dict | None = None,  # Accept but don't use (supervisor uses this)
    ) -> None:
        """Emit worker_tool_started event.

        Always emits worker_tool_started - identity is fixed at construction.
        """
        if not self.run_id:
            logger.debug("Skipping emit_tool_started: no run_id")
            return

        from zerg.services.event_store import append_run_event

        try:
            await append_run_event(
                run_id=self.run_id,
                event_type="worker_tool_started",
                payload={
                    "worker_id": self.worker_id,
                    "owner_id": self.owner_id,
                    "job_id": self.job_id,  # Critical for roundabout correlation
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_args_preview": tool_args_preview,
                    "trace_id": self.trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit worker_tool_started event", exc_info=True)

    async def emit_tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        result_preview: str,
        result: str | None = None,  # Accept but don't use (supervisor uses this)
    ) -> None:
        """Emit worker_tool_completed event.

        Always emits worker_tool_completed - identity is fixed at construction.
        """
        if not self.run_id:
            logger.debug("Skipping emit_tool_completed: no run_id")
            return

        from zerg.services.event_store import append_run_event

        try:
            await append_run_event(
                run_id=self.run_id,
                event_type="worker_tool_completed",
                payload={
                    "worker_id": self.worker_id,
                    "owner_id": self.owner_id,
                    "job_id": self.job_id,  # Critical for roundabout correlation
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                    "result_preview": result_preview,
                    "trace_id": self.trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit worker_tool_completed event", exc_info=True)

    async def emit_tool_failed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        error: str,
    ) -> None:
        """Emit worker_tool_failed event.

        Always emits worker_tool_failed - identity is fixed at construction.
        """
        if not self.run_id:
            logger.debug("Skipping emit_tool_failed: no run_id")
            return

        from zerg.services.event_store import append_run_event

        try:
            await append_run_event(
                run_id=self.run_id,
                event_type="worker_tool_failed",
                payload={
                    "worker_id": self.worker_id,
                    "owner_id": self.owner_id,
                    "job_id": self.job_id,  # Critical for roundabout correlation
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                    "error": error[:500] if error else None,  # Truncate for safety
                    "trace_id": self.trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit worker_tool_failed event", exc_info=True)

    async def emit_heartbeat(
        self,
        activity: str,
        phase: str,
    ) -> None:
        """Emit worker_heartbeat event during long-running LLM calls.

        Always emits worker_heartbeat - identity is fixed at construction.
        """
        from zerg.events.event_bus import EventType
        from zerg.events.event_bus import event_bus

        try:
            await event_bus.publish(
                EventType.WORKER_HEARTBEAT,
                {
                    "event_type": EventType.WORKER_HEARTBEAT,
                    "worker_id": self.worker_id,
                    "owner_id": self.owner_id,
                    "run_id": self.run_id,
                    "job_id": self.job_id,
                    "activity": activity,
                    "phase": phase,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.debug(f"Emitted heartbeat for worker {self.worker_id} during {phase}")
        except Exception:
            logger.warning("Failed to emit worker_heartbeat event", exc_info=True)


__all__ = ["WorkerEmitter", "ToolCall"]
