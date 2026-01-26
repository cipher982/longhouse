"""ConciergeEmitter - emits concierge_tool_* events with identity baked in.

This class replaces the contextvar-based event emission pattern. The emitter's
identity (concierge) is fixed at construction time, so it always emits the
correct event type regardless of contextvar state.

Key design principle: The emitter does NOT hold a DB session. Event emission
uses append_course_event() which opens its own short-lived session. This prevents
DB sessions from crossing async/thread boundaries via contextvars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

logger = logging.getLogger(__name__)


@dataclass
class ConciergeEmitter:
    """Emitter for concierge tool events - identity baked at construction.

    Always emits concierge_tool_* events, regardless of contextvar state.
    This eliminates the contextvar leakage bug where commis events could
    contaminate concierge event emission.

    Key principle: No DB session stored. Event emission uses append_course_event()
    which opens its own short-lived session per event.

    Attributes
    ----------
    course_id
        Concierge Course ID for event correlation
    owner_id
        User ID that owns this concierge fiche
    message_id
        UUID for the assistant message (stable across tokens/completion)
    """

    course_id: int
    owner_id: int
    message_id: str
    trace_id: str | None = None

    @property
    def is_commis(self) -> bool:
        """Always False - this is a concierge emitter."""
        return False

    @property
    def is_concierge(self) -> bool:
        """Always True - this is a concierge emitter."""
        return True

    async def emit_tool_started(
        self,
        tool_name: str,
        tool_call_id: str,
        tool_args_preview: str,
        tool_args: dict | None = None,
    ) -> None:
        """Emit concierge_tool_started event.

        Always emits concierge_tool_started - identity is fixed at construction.
        """
        from zerg.services.event_store import append_course_event

        try:
            await append_course_event(
                course_id=self.course_id,
                event_type="concierge_tool_started",
                payload={
                    "owner_id": self.owner_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_args_preview": tool_args_preview,
                    "tool_args": tool_args,  # Full args for persistence/raw view
                    "trace_id": self.trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit concierge_tool_started event", exc_info=True)

    async def emit_tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        result_preview: str,
        result: str | dict | None = None,
    ) -> None:
        """Emit concierge_tool_completed event.

        Always emits concierge_tool_completed - identity is fixed at construction.
        """
        from zerg.services.event_store import append_course_event

        try:
            # Handle structured results (dict) vs string results differently
            if isinstance(result, dict):
                # Structured result (e.g., spawn_commis with job_id) - pass through
                result_payload = result
            elif isinstance(result, str):
                # String result - truncate and wrap for backward compatibility
                raw_result = result[:2000] if len(result) > 2000 else result
                result_payload = {"raw": raw_result}
            else:
                result_payload = None

            await append_course_event(
                course_id=self.course_id,
                event_type="concierge_tool_completed",
                payload={
                    "owner_id": self.owner_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                    "result_preview": result_preview,
                    "result": result_payload,
                    "trace_id": self.trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit concierge_tool_completed event", exc_info=True)

    async def emit_tool_failed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        error: str,
    ) -> None:
        """Emit concierge_tool_failed event.

        Always emits concierge_tool_failed - identity is fixed at construction.
        """
        from zerg.services.event_store import append_course_event

        try:
            await append_course_event(
                course_id=self.course_id,
                event_type="concierge_tool_failed",
                payload={
                    "owner_id": self.owner_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "duration_ms": duration_ms,
                    "error": error[:500] if error else None,  # Truncate for safety
                    "error_details": {"raw_error": error},
                    "trace_id": self.trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning("Failed to emit concierge_tool_failed event", exc_info=True)

    async def emit_heartbeat(
        self,
        activity: str,
        phase: str,
    ) -> None:
        """Emit concierge_heartbeat event during long-running LLM calls.

        Always emits concierge_heartbeat - identity is fixed at construction.
        """
        from zerg.events.event_bus import EventType
        from zerg.events.event_bus import event_bus

        try:
            await event_bus.publish(
                EventType.CONCIERGE_HEARTBEAT,
                {
                    "event_type": EventType.CONCIERGE_HEARTBEAT,
                    "course_id": self.course_id,
                    "owner_id": self.owner_id,
                    "activity": activity,
                    "phase": phase,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.debug(f"Emitted heartbeat for concierge run {self.course_id} during {phase}")
        except Exception:
            logger.warning("Failed to emit concierge_heartbeat event", exc_info=True)


# Keep ConciergeEmitter as alias for backward compatibility during migration
ConciergeEmitter = ConciergeEmitter

__all__ = ["ConciergeEmitter", "ConciergeEmitter"]
