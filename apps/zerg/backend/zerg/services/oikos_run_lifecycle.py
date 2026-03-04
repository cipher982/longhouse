"""Shared Oikos run lifecycle helpers.

These helpers centralize small pieces of run lifecycle behavior used by both
oikos_service and commis_resume to reduce drift between primary and
continuation paths.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from zerg.models.models import CommisJob


async def emit_stream_control_for_pending_commiss(
    db: Session,
    run: Any,
    owner_id: int,
    *,
    ttl_ms: int = 120_000,
) -> int:
    """Emit stream lifecycle control based on pending commis jobs.

    - If there are queued/running commiss for this run, keep the stream open.
    - Otherwise, close the stream as all delegated work is complete.

    Returns:
        Number of pending commis jobs at the time of check.
    """
    from zerg.services.oikos_service import emit_stream_control

    pending_commiss_count = (
        db.query(CommisJob)
        .filter(
            CommisJob.oikos_run_id == run.id,
            CommisJob.status.in_(["queued", "running"]),
        )
        .count()
    )
    if pending_commiss_count > 0:
        await emit_stream_control(db, run, "keep_open", "commiss_pending", owner_id, ttl_ms=ttl_ms)
    else:
        await emit_stream_control(db, run, "close", "all_complete", owner_id)

    return pending_commiss_count
