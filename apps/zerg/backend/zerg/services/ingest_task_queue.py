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
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.database import get_session_factory
from zerg.models.agents import SessionTask

logger = logging.getLogger(__name__)

WORKER_POLL_SECONDS = 2.0
BATCH_SIZE = 5
STALE_RUNNING_MINUTES = 30
_OPERATOR_COMPLETION_FRESH_WINDOW = timedelta(minutes=10)
_OPERATOR_COMPLETION_SKIP_STATES = {"thinking", "running", "needs_user", "blocked"}


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
    pending_query = db.query(SessionTask).filter(SessionTask.status == "pending")
    pending = pending_query.order_by(SessionTask.created_at).limit(limit).all()
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


def _operator_mode_enabled() -> bool:
    return os.getenv("OIKOS_OPERATOR_MODE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _build_operator_completion_message(
    *,
    session_id: str,
    provider: str | None,
    project: str | None,
    cwd: str | None,
    ended_at: datetime,
    summary_title: str | None,
    summary: str | None,
    presence_state: str | None,
) -> str:
    lines = [
        "System/operator wakeup: a coding session completed a new ingested turn.",
        "",
        "Trigger: session_completed",
        f"Session ID: {session_id}",
        f"Ended At: {ended_at.isoformat()}",
    ]
    if provider:
        lines.append(f"Provider: {provider}")
    if project:
        lines.append(f"Project: {project}")
    if cwd:
        lines.append(f"CWD: {cwd}")
    if presence_state:
        lines.append(f"Presence: {presence_state}")
    if summary_title:
        lines.append(f"Summary Title: {summary_title}")
    if summary:
        lines.append(f"Summary: {summary}")
    lines.extend(
        [
            "",
            "Inspect the latest session history, then decide whether to wait, continue, or escalate.",
            "Do nothing if no action is warranted.",
        ]
    )
    return "\n".join(lines)


async def _maybe_invoke_operator_completion_wakeup(task_id: str, session_id: str) -> None:
    if not _operator_mode_enabled():
        return

    from zerg.models.agents import AgentSession
    from zerg.models.agents import SessionPresence
    from zerg.models.user import User

    factory = get_session_factory()
    db = factory()
    try:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if session is None:
            return
        if session.user_state in {"archived", "snoozed"}:
            return

        ended_at = _normalize_utc(session.ended_at)
        if ended_at is None:
            return

        now = datetime.now(timezone.utc)
        if now - ended_at > _OPERATOR_COMPLETION_FRESH_WINDOW:
            return

        presence = db.query(SessionPresence).filter(SessionPresence.session_id == session_id).first()
        presence_state: str | None = None
        if presence is not None:
            updated_at = _normalize_utc(presence.updated_at)
            if updated_at is not None and (now - updated_at) < _OPERATOR_COMPLETION_FRESH_WINDOW:
                presence_state = presence.state

        if presence_state in _OPERATOR_COMPLETION_SKIP_STATES:
            return

        owner = db.query(User.id).order_by(User.id).first()
        if owner is None:
            return
        owner_id = int(owner[0])

        provider = session.provider
        project = session.project
        cwd = session.cwd
        summary_title = session.summary_title
        summary = session.summary
    finally:
        db.close()

    from zerg.services.oikos_service import invoke_oikos
    from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter

    message = _build_operator_completion_message(
        session_id=session_id,
        provider=provider,
        project=project,
        cwd=cwd,
        ended_at=ended_at,
        summary_title=summary_title,
        summary=summary,
        presence_state=presence_state,
    )
    message_id = f"operator-session-completed-{session_id}-{task_id}"
    surface_payload = {
        "trigger_type": "session_completed",
        "session_id": session_id,
        "provider": provider,
        "project": project,
        "cwd": cwd,
        "ended_at": ended_at.isoformat(),
        "presence_state": presence_state,
        "summary_title": summary_title,
    }

    try:
        await invoke_oikos(
            owner_id,
            message,
            message_id,
            source="operator",
            surface_adapter=OperatorSurfaceAdapter(owner_id=owner_id),
            surface_payload=surface_payload,
        )
    except Exception:
        logger.exception("Failed to invoke operator completion wakeup for session %s", session_id)


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
        if task_type == "summary":
            await _maybe_invoke_operator_completion_wakeup(task_id, session_id)
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
