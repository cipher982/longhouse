"""SSE streaming helpers for Jarvis.

This module provides a thin wrapper around the unified streaming implementation
in stream.py. The Jarvis chat uses live-only streaming (no replay) with
continuation run support.

Prior to consolidation, this module had its own streaming implementation with
~150 lines of duplicated logic. Now it delegates to stream_run_events_live().
"""

from zerg.routers.stream import stream_run_events_live

__all__ = ["stream_run_events"]


async def stream_run_events(
    run_id: int,
    owner_id: int,
):
    """Generate SSE events for a specific run (live-only, no replay).

    Delegates to the unified streaming implementation in stream.py.
    This is used for Jarvis chat initial connection.

    For reconnection/replay, clients should use GET /api/stream/runs/{run_id}
    directly with Last-Event-ID header.

    Args:
        run_id: Run identifier
        owner_id: Owner ID for security filtering

    Yields:
        SSE events in format: {"event": str, "data": str}
    """
    async for event in stream_run_events_live(run_id, owner_id):
        yield event
