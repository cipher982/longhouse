"""Durable task queue for post-ingest background work.

Replaces FastAPI BackgroundTasks for summary and embedding generation so tasks
survive process restarts. A single asyncio worker polls this table, retrying
failures up to max_attempts.

Architecture:
- Single-worker design: one asyncio task processes tasks sequentially.
  No row-level locking needed because only one worker runs per process.
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

from zerg.database import get_session_factory
from zerg.models.agents import SessionTask

logger = logging.getLogger(__name__)

WORKER_POLL_SECONDS = 2.0
BATCH_SIZE = 5
STALE_RUNNING_MINUTES = 30


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def enqueue_ingest_tasks(db, session_id: str) -> None:
    """Insert summary + embedding tasks for session (deduped, no commit — caller commits)."""
    for task_type in ("summary", "embedding"):
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
    logger.info("Ingest task worker started (poll=%.1fs, batch=%d)", poll_seconds, BATCH_SIZE)
    while True:
        try:
            await _process_batch()
        except Exception:
            logger.exception("Ingest task worker: unexpected error in batch")
        await asyncio.sleep(poll_seconds)


async def _process_batch() -> None:
    factory = get_session_factory()
    db = factory()
    try:
        tasks = _claim_pending(db, BATCH_SIZE)
    finally:
        db.close()

    if not tasks:
        return

    # Execute sequentially to avoid hammering the LLM API
    for task_id, session_id, task_type in tasks:
        await _execute_task(task_id, session_id, task_type)


def _claim_pending(db, limit: int) -> list[tuple[str, str, str]]:
    """Mark pending tasks as running; return (id, session_id, task_type) tuples."""
    pending = db.query(SessionTask).filter(SessionTask.status == "pending").order_by(SessionTask.created_at).limit(limit).all()
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


async def _execute_task(task_id: str, session_id: str, task_type: str) -> None:
    try:
        if task_type == "summary":
            from zerg.routers.agents import _generate_summary_impl

            await _generate_summary_impl(session_id)
        elif task_type == "embedding":
            from zerg.routers.agents import _generate_embeddings_impl

            await _generate_embeddings_impl(session_id)
        else:
            logger.warning("Unknown task_type %r for task %s", task_type, task_id)

        _mark_status(task_id, "done", error=None, retry=False)
        logger.debug("Ingest task %s (%s/%s) done", task_id, task_type, session_id)
    except Exception as e:
        logger.exception("Ingest task %s (%s/%s) failed", task_id, task_type, session_id)
        _mark_status(task_id, "failed", error=str(e), retry=True)


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
