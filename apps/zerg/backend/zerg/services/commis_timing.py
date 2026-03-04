"""Shared timing helpers for commis continuation flows."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone


def compute_duration_ms(started_at, *, end_time: datetime | None = None) -> int:
    """Compute elapsed milliseconds from a naive UTC `started_at` timestamp."""
    if started_at is None:
        return 0
    end_dt = end_time or datetime.now(timezone.utc)
    try:
        started_dt = started_at.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    return max(0, int((end_dt - started_dt).total_seconds() * 1000))
