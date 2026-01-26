"""EventEmitter Protocol - defines the interface for tool event emission.

Emitters have their identity (commis vs concierge) baked in at construction.
This eliminates contextvar leakage bugs where events get misclassified.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Protocol
from typing import runtime_checkable

if TYPE_CHECKING:
    pass


@runtime_checkable
class EventEmitter(Protocol):
    """Protocol for tool event emission.

    Identity is baked at construction time. A CommisEmitter will always
    emit commis_tool_* events, a ConciergeEmitter will always emit
    concierge_tool_* events, regardless of contextvar state.

    Usage:
        emitter = get_emitter()
        if emitter:
            await emitter.emit_tool_started(tool_name, tool_call_id, args_preview)
            # ... execute tool ...
            await emitter.emit_tool_completed(tool_name, tool_call_id, duration_ms, result_preview)
    """

    @property
    def is_commis(self) -> bool:
        """True if this emitter is for commis context."""
        ...

    @property
    def is_concierge(self) -> bool:
        """True if this emitter is for concierge context."""
        ...

    async def emit_tool_started(
        self,
        tool_name: str,
        tool_call_id: str,
        tool_args_preview: str,
        tool_args: dict | None = None,
    ) -> None:
        """Emit tool_started event with correct type for this emitter's identity."""
        ...

    async def emit_tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        result_preview: str,
        result: str | None = None,
    ) -> None:
        """Emit tool_completed event with correct type for this emitter's identity."""
        ...

    async def emit_tool_failed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        error: str,
    ) -> None:
        """Emit tool_failed event with correct type for this emitter's identity."""
        ...

    async def emit_heartbeat(
        self,
        activity: str,
        phase: str,
    ) -> None:
        """Emit heartbeat event during long-running operations (LLM calls)."""
        ...


__all__ = ["EventEmitter"]
