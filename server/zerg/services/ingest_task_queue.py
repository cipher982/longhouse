"""Durable task queue for post-ingest background work.

Replaces FastAPI BackgroundTasks for summary generation, embeddings, and
turn-loop evaluation so tasks survive process restarts. A single asyncio
worker polls this table, retrying failures up to max_attempts.

Architecture:
- Single-worker design: one asyncio task processes tasks sequentially.
  No row-level locking needed because only one worker runs per process.
- Claim one task at a time so newly arrived turn-loop work can preempt older
  low-priority tasks on the next iteration.
- Bound task execution time so a hung summary/embedding call cannot stall the
  entire post-ingest pipeline indefinitely.
- Crash recovery: on startup, stale 'running' tasks are reset to 'pending'.
- Dedup: won't enqueue a duplicate pending/running task for the same session+type.

Usage:
    # In ingest endpoint — replaces background_tasks.add_task():
    enqueue_ingest_tasks(db, session_id)  # then caller commits db

    # In lifespan:
    reset_stale_running_tasks(db)
    asyncio.create_task(run_ingest_task_worker())
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import case

from zerg.database import get_session_factory
from zerg.models.agents import SessionTask
from zerg.services.session_turn_reviews import maybe_process_session_turn_loop
from zerg.services.session_turn_reviews import turn_loop_retry_needed

logger = logging.getLogger(__name__)

WORKER_POLL_SECONDS = 2.0
CLAIM_LIMIT = 1
STALE_RUNNING_MINUTES = 30
TASK_TIMEOUT_SECONDS: dict[str, float] = {
    "turn_loop": 60.0,
    "summary": 180.0,
    "embedding": 30.0,
}


class RetryTaskLater(Exception):
    """Signal that a task should be re-queued without treating it as a hard failure."""


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def enqueue_ingest_tasks(db, session_id: str) -> None:
    """Insert summary + embedding + turn-loop tasks for session (deduped, caller commits)."""
    for task_type in ("summary", "embedding", "turn_loop"):
        _enqueue_if_not_active(db, session_id, task_type)


def _enqueue_if_not_active(db, session_id: str, task_type: str) -> None:
    existing = (
        db.query(SessionTask.id)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.task_type == task_type,
            SessionTask.status.in_(["pending", "running"]),
        )
        .first()
    )
    if existing:
        logger.debug("Skipping duplicate %s task for session %s", task_type, session_id)
        return
    db.add(SessionTask(session_id=session_id, task_type=task_type))


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


def reset_stale_running_tasks(db) -> int:
    """Reset tasks stuck as 'running' from a previous crashed process.

    Call once at startup before starting the worker.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_MINUTES)
    count = (
        db.query(SessionTask)
        .filter(SessionTask.status == "running", SessionTask.updated_at < cutoff)
        .update({"status": "pending", "updated_at": datetime.now(timezone.utc)})
    )
    if count:
        logger.info("Recovered %d stale running ingest tasks", count)
    db.commit()
    return count


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


async def run_ingest_task_worker(poll_seconds: float = WORKER_POLL_SECONDS) -> None:
    """Background worker: poll and execute pending ingest tasks.

    Runs indefinitely. Launch as asyncio.create_task() from lifespan.
    """
    logger.info("Ingest task worker started (poll=%.1fs, claim_limit=%d)", poll_seconds, CLAIM_LIMIT)
    while True:
        try:
            await _process_batch()
        except Exception:
            logger.exception("Ingest task worker: unexpected error in batch")
        await asyncio.sleep(poll_seconds)


async def _process_batch() -> None:
    while True:
        factory = get_session_factory()
        db = factory()
        try:
            tasks = _claim_pending(db, CLAIM_LIMIT)
        finally:
            db.close()

        if not tasks:
            return

        task_id, session_id, task_type = tasks[0]
        retried = await _execute_task(task_id, session_id, task_type)
        if retried:
            # Task was re-queued; break so the outer worker sleep provides a
            # yield before we re-claim — prevents event-loop starvation.
            break


def _claim_pending(db, limit: int) -> list[tuple[str, str, str]]:
    """Mark pending tasks as running; return (id, session_id, task_type) tuples."""
    priority = case(
        (SessionTask.task_type == "turn_loop", 0),
        (SessionTask.task_type == "summary", 1),
        else_=2,
    )
    pending_query = db.query(SessionTask).filter(SessionTask.status == "pending")
    # RetryLater paths bump updated_at when they yield, so claim by updated_at
    # before created_at to keep a single re-queued task from pinning the queue.
    pending = pending_query.order_by(priority, SessionTask.updated_at, SessionTask.created_at, SessionTask.id).limit(limit).all()
    if not pending:
        return []

    now = datetime.now(timezone.utc)
    claimed = []
    for task in pending:
        task.status = "running"
        task.attempts = (task.attempts or 0) + 1
        task.updated_at = now
        claimed.append((task.id, task.session_id, task.task_type))
    db.commit()
    return claimed


async def _execute_task(task_id: str, session_id: str, task_type: str) -> bool:
    """Execute a single task. Returns True if the task was re-queued (RetryTaskLater)."""
    timeout_seconds = TASK_TIMEOUT_SECONDS.get(task_type)
    try:
        if timeout_seconds is None:
            await _run_task_impl(task_id, session_id, task_type)
        else:
            await asyncio.wait_for(_run_task_impl(task_id, session_id, task_type), timeout=timeout_seconds)

        _mark_status(task_id, "done", error=None, retry=False)
        logger.debug("Ingest task %s (%s/%s) done", task_id, task_type, session_id)
        return False
    except RetryTaskLater as e:
        logger.info("Ingest task %s (%s/%s) re-queued: %s", task_id, task_type, session_id, e)
        # RetryTaskLater means "not yet" (session still active), not a real failure.
        # Reset to pending WITHOUT consuming the retry budget so we never drop a
        # turn review just because the session was actively running for >6 seconds.
        _reset_for_retry_later(task_id, str(e))
        return True
    except asyncio.TimeoutError:
        timeout_label = f"{timeout_seconds:g}s" if timeout_seconds is not None else "unknown"
        logger.warning("Ingest task %s (%s/%s) timed out after %s", task_id, task_type, session_id, timeout_label)
        _mark_status(
            task_id,
            "failed",
            error=f"{task_type} task timed out after {timeout_label}",
            retry=True,
        )
        return False
    except Exception as e:
        logger.exception("Ingest task %s (%s/%s) failed", task_id, task_type, session_id)
        _mark_status(task_id, "failed", error=str(e), retry=True)
        return False


async def _run_task_impl(task_id: str, session_id: str, task_type: str) -> None:
    if task_type == "summary":
        from zerg.routers.agents import _generate_summary_impl

        await _generate_summary_impl(session_id)
        return
    if task_type == "embedding":
        from zerg.routers.agents import _generate_embeddings_impl

        await _generate_embeddings_impl(session_id)
        return
    if task_type == "turn_loop":
        factory = get_session_factory()
        db = factory()
        try:
            task = db.query(SessionTask).filter(SessionTask.id == task_id).first()
            review = await maybe_process_session_turn_loop(
                db=db,
                session_id=session_id,
                freshness_reference_at=getattr(task, "created_at", None),
            )
            if review is None and turn_loop_retry_needed(
                db=db,
                session_id=session_id,
                freshness_reference_at=getattr(task, "created_at", None),
            ):
                raise RetryTaskLater("waiting for active session presence to settle before creating turn review")
        finally:
            db.close()
        return
    logger.warning("Unknown task_type %r for session %s", task_type, session_id)


def _reset_for_retry_later(task_id: str, error: str) -> None:
    """Reset a task to pending without consuming its retry budget.

    Used by RetryTaskLater — the signal means "not yet" (e.g. session is still
    actively running), not a real failure. We undo the attempt increment that
    _claim_pending applied so the retry budget is preserved for genuine errors.
    """
    factory = get_session_factory()
    db = factory()
    try:
        task = db.query(SessionTask).filter(SessionTask.id == task_id).first()
        if not task:
            return
        # Undo the attempt increment from _claim_pending
        task.attempts = max(0, (task.attempts or 1) - 1)
        task.status = "pending"
        task.error = error[:1000] if error else None
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        logger.exception("Failed to reset task %s for retry later", task_id)
        db.rollback()
    finally:
        db.close()


def _mark_status(task_id: str, final_status: str, error: str | None, retry: bool) -> None:
    factory = get_session_factory()
    db = factory()
    try:
        task = db.query(SessionTask).filter(SessionTask.id == task_id).first()
        if not task:
            return
        if retry and task.attempts < task.max_attempts:
            task.status = "pending"
            logger.info("Task %s re-queued (attempt %d/%d)", task_id, task.attempts, task.max_attempts)
        else:
            task.status = final_status
            if retry:
                logger.warning("Task %s exhausted %d attempts → failed", task_id, task.max_attempts)
        # Always overwrite error: clears stale error on success, records new on failure
        task.error = error[:1000] if error is not None else None
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        logger.exception("Failed to update task %s status", task_id)
        db.rollback()
    finally:
        db.close()
