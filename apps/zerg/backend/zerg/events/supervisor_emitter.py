"""SupervisorEmitter - emits supervisor_tool_* events with identity baked in.

This class replaces the contextvar-based event emission pattern. The emitter's
identity (supervisor) is fixed at construction time, so it always emits the
correct event type regardless of contextvar state.

Key design principle: The emitter does NOT hold a DB session. Event emission
uses append_run_event() which opens its own short-lived session. This prevents
DB sessions from crossing async/thread boundaries via contextvars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

logger = logging.getLogger(__name__)


@dataclass
class SupervisorEmitter:
    """Emitter for supervisor tool events - identity baked at construction.

    Always emits supervisor_tool_* events, regardless of contextvar state.
    This eliminates the contextvar leakage bug where worker events could
    contaminate supervisor event emission.

    Key principle: No DB session stored. Event emission uses append_run_event()
    which opens its own short-lived session per event.

    Attributes
    ----------
    run_id
        Supervisor AgentRun ID for event correlation
    owner_id
        User ID that owns this supervisor agent
    message_id
        UUID for the assistant message (stable across tokens/completion)
    """

    run_id: int
    owner_id: int
    message_id: str

    @property
    def is_worker(self) -> bool:
        """Always False - this is a supervisor emitter."""
        return False

    @property
    def is_supervisor(self) -> bool:
        """Always True - this is a supervisor emitter."""
        return True

    async def emit_tool_started(
        self,
        tool_name: str,
        tool_call_id: str,
        tool_args_preview: str,
        tool_args: dict | None = None,
    ) -> None:
        """Emit supervisor_tool_started event.

        Always emits supervisor_tool_started - identity is fixed at construction.
        """
        from zerg.services.event_store import append_run_event

        try:
            await append_run_event(
                run_id=self.run_id,
                event_type="supervisor_tool_started",
                payload={
                    "owner_id": self.owner_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_args_preview": tool_args_preview,
                    "tool_args": tool_args,  # Full args for persistence/raw view
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit supervisor_tool_started event", exc_info=True)

    async def emit_tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        result_preview: str,
        result: str | dict | None = None,
    ) -> None:
        """Emit supervisor_tool_completed event.

        Always emits supervisor_tool_completed - identity is fixed at construction.
        """
        from zerg.services.event_store import append_run_event

        try:
            # Handle structured results (dict) vs string results differently
            if isinstance(result, dict):
                # Structured result (e.g., spawn_worker with job_id) - pass through
                result_payload = result
            elif isinstance(result, str):
                # String result - truncate and wrap for backward compatibility
                raw_result = result[:2000] if len(result) > 2000 else result
                result_payload = {"raw": raw_result}
            else:
                result_payload = None

            await append_run_event(
                run_id=self.run_id,
                event_type="supervisor_tool_completed",
                payload={
                    "owner_id": self.owner_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                    "result_preview": result_preview,
                    "result": result_payload,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit supervisor_tool_completed event", exc_info=True)

    async def emit_tool_failed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        error: str,
    ) -> None:
        """Emit supervisor_tool_failed event.

        Always emits supervisor_tool_failed - identity is fixed at construction.
        """
        from zerg.services.event_store import append_run_event

        try:
            await append_run_event(
                run_id=self.run_id,
                event_type="supervisor_tool_failed",
                payload={
                    "owner_id": self.owner_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                    "error": error[:500] if error else None,  # Truncate for safety
                    "error_details": {"raw_error": error},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit supervisor_tool_failed event", exc_info=True)

    async def emit_heartbeat(
        self,
        activity: str,
        phase: str,
    ) -> None:
        """Emit supervisor_heartbeat event during long-running LLM calls.

        Always emits supervisor_heartbeat - identity is fixed at construction.
        """
        from zerg.events.event_bus import EventType
        from zerg.events.event_bus import event_bus

        try:
            await event_bus.publish(
                EventType.SUPERVISOR_HEARTBEAT,
                {
                    "event_type": EventType.SUPERVISOR_HEARTBEAT,
                    "run_id": self.run_id,
                    "owner_id": self.owner_id,
                    "activity": activity,
                    "phase": phase,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.debug(f"Emitted heartbeat for supervisor run {self.run_id} during {phase}")
        except Exception:
            logger.warning("Failed to emit supervisor_heartbeat event", exc_info=True)


__all__ = ["SupervisorEmitter"]
