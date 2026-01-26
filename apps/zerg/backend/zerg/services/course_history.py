"""
Course History Service: Consolidates Course lifecycle logic for thread-based courses.
"""

from datetime import datetime
from datetime import timezone
from typing import Sequence

from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.managers.fiche_runner import FicheRunner
from zerg.models.models import Fiche as FicheModel
from zerg.models.models import Thread as ThreadModel


async def execute_thread_course_with_history(
    db: Session,
    fiche: FicheModel,
    thread: ThreadModel,
    runner: FicheRunner,
    trigger: str = "api",
) -> Sequence:
    """
    Execute a single course of the fiche on the given thread,
    recording Course rows and publishing course events.

    Returns the sequence of created message rows from FicheRunner.run_thread().
    """
    # Create the Course (queued)
    course_row = crud.create_course(
        db,
        fiche_id=fiche.id,
        thread_id=thread.id,
        trigger=trigger,
        status="queued",
    )
    # Notify queued state
    await event_bus.publish(
        EventType.COURSE_CREATED,
        {
            "event_type": "course_created",
            "fiche_id": fiche.id,
            "course_id": course_row.id,
            "status": course_row.status,
            "thread_id": thread.id,
        },
    )

    # Mark running
    start_ts = datetime.now(timezone.utc)
    crud.mark_course_running(db, course_row.id, started_at=start_ts)
    await event_bus.publish(
        EventType.COURSE_UPDATED,
        {
            "event_type": "course_updated",
            "fiche_id": fiche.id,
            "course_id": course_row.id,
            "status": "running",
            "started_at": start_ts.isoformat(),
            "thread_id": thread.id,
        },
    )

    # Execute the fiche turn
    try:
        created_rows = await runner.run_thread(db, thread)
    except Exception as exc:
        # Failure path
        end_ts = datetime.now(timezone.utc)
        duration_ms = int((end_ts - start_ts).total_seconds() * 1000)
        crud.mark_course_failed(db, course_row.id, finished_at=end_ts, duration_ms=duration_ms, error=str(exc))
        await event_bus.publish(
            EventType.COURSE_UPDATED,
            {
                "event_type": "course_updated",
                "fiche_id": fiche.id,
                "course_id": course_row.id,
                "status": "failed",
                "finished_at": end_ts.isoformat(),
                "duration_ms": duration_ms,
                "error": str(exc),
                "thread_id": thread.id,
            },
        )
        raise

    # Success path
    end_ts = datetime.now(timezone.utc)
    duration_ms = int((end_ts - start_ts).total_seconds() * 1000)
    # Persist usage/cost if Runner captured metadata
    total_tokens = getattr(runner, "usage_total_tokens", None)
    total_cost_usd = None
    if getattr(runner, "usage_prompt_tokens", None) is not None and getattr(runner, "usage_completion_tokens", None) is not None:
        from zerg.pricing import get_usd_prices_per_1k

        prices = get_usd_prices_per_1k(fiche.model)
        if prices is not None:
            in_price, out_price = prices
            total_cost_usd = ((runner.usage_prompt_tokens * in_price) + (runner.usage_completion_tokens * out_price)) / 1000.0

    # Mark course as finished (summary auto-extracted)
    finished_course = crud.mark_course_finished(
        db,
        course_row.id,
        finished_at=end_ts,
        duration_ms=duration_ms,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
    )

    # Refresh to get the auto-extracted summary
    if finished_course:
        db.refresh(finished_course)

    await event_bus.publish(
        EventType.COURSE_UPDATED,
        {
            "event_type": "course_updated",
            "fiche_id": fiche.id,
            "course_id": course_row.id,
            "status": "success",
            "finished_at": end_ts.isoformat(),
            "duration_ms": duration_ms,
            "summary": finished_course.summary if finished_course else None,
            "thread_id": thread.id,
        },
    )

    # Auto-summary -> Memory Files (async, best-effort)
    from zerg.services.memory_summarizer import schedule_course_summary

    # Extract final result (last assistant message)
    result_text = None
    for row in reversed(created_rows):
        if row.role == "assistant" and row.content:
            result_text = row.content
            break

    # Extract task (last user message)
    task = None
    for row in created_rows:
        if row.role == "user" and row.content:
            task = row.content
            break

    schedule_course_summary(
        owner_id=fiche.owner_id,
        thread_id=thread.id,
        course_id=course_row.id,
        task=task or "",
        result_text=result_text or "",
        trace_id=str(course_row.trace_id) if hasattr(course_row, "trace_id") and course_row.trace_id else None,
    )

    return created_rows
