"""Shared in-memory runner heartbeat cache.

Keeps websocket heartbeat timestamps in one place so routers and health
assessments do not import each other.
"""

from __future__ import annotations

from datetime import datetime

runner_heartbeat_cache: dict[int, datetime] = {}


def mark_runner_heartbeat(runner_id: int, *, seen_at: datetime) -> None:
    """Record the latest heartbeat timestamp for a runner."""
    runner_heartbeat_cache[runner_id] = seen_at


def get_runner_heartbeat(runner_id: int | None) -> datetime | None:
    """Return cached heartbeat timestamp, if present."""
    if runner_id is None:
        return None
    return runner_heartbeat_cache.get(runner_id)
