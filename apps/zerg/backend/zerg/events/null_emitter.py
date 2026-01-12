"""NullEmitter - no-op emitter for testing and direct agent calls.

This emitter does nothing when emit methods are called. Use it for:
- Unit tests where event emission is not needed
- Direct agent calls outside supervisor/worker context
- Performance testing without event overhead
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class NullEmitter:
    """No-op emitter that discards all events.

    Use this for contexts where event emission is not needed:
    - Unit tests
    - Direct agent calls
    - Performance benchmarks

    All emit_* methods are no-ops (do nothing, return immediately).
    """

    @property
    def is_worker(self) -> bool:
        """Always False - this is a null emitter."""
        return False

    @property
    def is_supervisor(self) -> bool:
        """Always False - this is a null emitter."""
        return False

    async def emit_tool_started(
        self,
        tool_name: str,
        tool_call_id: str,
        tool_args_preview: str,
        tool_args: dict | None = None,
    ) -> None:
        """No-op: discard tool_started event."""
        pass

    async def emit_tool_completed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        result_preview: str,
        result: str | None = None,
    ) -> None:
        """No-op: discard tool_completed event."""
        pass

    async def emit_tool_failed(
        self,
        tool_name: str,
        tool_call_id: str,
        duration_ms: int,
        error: str,
        **kwargs,
    ) -> None:
        """No-op: discard tool_failed event."""
        pass

    async def emit_heartbeat(
        self,
        activity: str,
        phase: str,
    ) -> None:
        """No-op: discard heartbeat event."""
        pass


__all__ = ["NullEmitter"]
