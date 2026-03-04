"""Shared Oikos run lifecycle helpers.

These helpers centralize small pieces of run lifecycle behavior used by both
oikos_service and commis_resume to reduce drift between primary and
continuation paths.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from zerg.models.models import CommisJob


async def emit_oikos_waiting_and_run_updated(
    db: Session,
    *,
    run_id: int,
    fiche_id: int,
    thread_id: int,
    owner_id: int,
    message_id: str,
    message: str,
    trace_id: str | None = None,
    job_id: int | None = None,
    job_ids: list[int] | None = None,
) -> None:
    """Emit oikos_waiting followed by run_updated(waiting)."""
    from zerg.services.event_store import emit_run_event

    payload: dict[str, Any] = {
        "fiche_id": fiche_id,
        "thread_id": thread_id,
        "message": message,
        "owner_id": owner_id,
        "message_id": message_id,
        "close_stream": False,
    }
    if trace_id:
        payload["trace_id"] = trace_id
    if job_ids:
        payload["job_ids"] = job_ids
    else:
        payload["job_id"] = job_id

    await emit_run_event(
        db=db,
        run_id=run_id,
        event_type="oikos_waiting",
        payload=payload,
    )
    await emit_run_event(
        db=db,
        run_id=run_id,
        event_type="run_updated",
        payload={
            "fiche_id": fiche_id,
            "status": "waiting",
            "thread_id": thread_id,
            "owner_id": owner_id,
        },
    )


async def emit_failed_run_updated(
    db: Session,
    *,
    run_id: int,
    fiche_id: int,
    thread_id: int,
    owner_id: int,
    finished_at_iso: str,
    duration_ms: int,
    error: str,
) -> None:
    """Emit standardized run_updated payload for failed runs."""
    from zerg.services.event_store import emit_run_event

    await emit_run_event(
        db=db,
        run_id=run_id,
        event_type="run_updated",
        payload={
            "fiche_id": fiche_id,
            "status": "failed",
            "finished_at": finished_at_iso,
            "duration_ms": duration_ms,
            "error": error,
            "thread_id": thread_id,
            "owner_id": owner_id,
        },
    )


async def emit_success_run_updated(
    db: Session,
    *,
    run_id: int,
    fiche_id: int,
    thread_id: int,
    owner_id: int,
    finished_at_iso: str,
    duration_ms: int,
) -> None:
    """Emit standardized run_updated payload for successful runs."""
    from zerg.services.event_store import emit_run_event

    await emit_run_event(
        db=db,
        run_id=run_id,
        event_type="run_updated",
        payload={
            "fiche_id": fiche_id,
            "status": "success",
            "finished_at": finished_at_iso,
            "duration_ms": duration_ms,
            "thread_id": thread_id,
            "owner_id": owner_id,
        },
    )


async def emit_cancelled_run_updated(
    db: Session,
    *,
    run_id: int,
    fiche_id: int,
    thread_id: int,
    owner_id: int,
    finished_at_iso: str,
    duration_ms: int,
) -> None:
    """Emit standardized run_updated payload for cancelled runs."""
    from zerg.services.event_store import emit_run_event

    await emit_run_event(
        db=db,
        run_id=run_id,
        event_type="run_updated",
        payload={
            "fiche_id": fiche_id,
            "status": "cancelled",
            "finished_at": finished_at_iso,
            "duration_ms": duration_ms,
            "thread_id": thread_id,
            "owner_id": owner_id,
        },
    )


async def emit_error_event_and_close_stream(
    db: Session,
    run: Any,
    *,
    thread_id: int,
    owner_id: int,
    message: str,
    trace_id: str | None = None,
    fiche_id: int | None = None,
    debug_url: str | None = None,
) -> None:
    """Emit an error event and close stream for terminal failures."""
    from zerg.services.event_store import emit_run_event
    from zerg.services.oikos_service import emit_stream_control

    payload: dict[str, Any] = {
        "thread_id": thread_id,
        "message": message,
        "status": "error",
        "owner_id": owner_id,
    }
    if trace_id:
        payload["trace_id"] = trace_id
    if fiche_id is not None:
        payload["fiche_id"] = fiche_id
    if debug_url:
        payload["debug_url"] = debug_url

    await emit_run_event(
        db=db,
        run_id=run.id,
        event_type="error",
        payload=payload,
    )

    await emit_stream_control(db, run, "close", "error", owner_id)


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
