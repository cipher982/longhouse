"""SSE streaming helpers for Jarvis.

This module provides a thin wrapper around the unified streaming implementation
in stream.py. The Jarvis chat uses live-only streaming (no replay) with
continuation course support.

Prior to consolidation, this module had its own streaming implementation with
~150 lines of duplicated logic. Now it delegates to stream_course_events_live().
"""

from zerg.routers.stream import stream_course_events_live

__all__ = ["stream_course_events"]


async def stream_course_events(
    course_id: int,
    owner_id: int,
):
    """Generate SSE events for a specific course (live-only, no replay).

    Delegates to the unified streaming implementation in stream.py.
    This is used for Jarvis chat initial connection.

    For reconnection/replay, clients should use GET /api/stream/courses/{course_id}
    directly with Last-Event-ID header.

    Args:
        course_id: Course identifier
        owner_id: Owner ID for security filtering

    Yields:
        SSE events in format: {"event": str, "data": str}
    """
    async for event in stream_course_events_live(course_id, owner_id):
        yield event
