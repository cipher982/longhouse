"""Durable task queue for post-ingest background work.

Replaces FastAPI BackgroundTasks for summary generation, embeddings, and
turn-loop evaluation so tasks survive process restarts. A single asyncio
worker polls this table, retrying failures up to max_attempts.

Architecture:
- Dual-lane design: one hot worker handles turn-loop work and one cold worker
  handles everything else. Claims still run through the single write serializer,
  so no row-level locking is needed inside the worker.
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
    asyncio.create_task(run_ingest_task_worker(worker_name="hot", include_task_types=("turn_loop",)))
    asyncio.create_task(run_ingest_task_worker(worker_name="cold", exclude_task_types=("turn_loop",)))
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import or_

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services.write_serializer import get_write_serializer

logger = logging.getLogger(__name__)

WORKER_POLL_SECONDS = 2.0
HOT_WORKER_POLL_SECONDS = 0.5
CLAIM_LIMIT = 1
COLD_WORKER_CONCURRENCY = int(os.getenv("COLD_INGEST_WORKER_CONCURRENCY", "1"))
HOT_WORKER_CONCURRENCY = 1
MAX_ATTEMPTS_DEFAULT = 5
STALE_RUNNING_MINUTES = 30
STALE_PENDING_SWEEP_BATCH = 1000
# Tiered per-attempt timeouts. Fail-fast on attempt 1, ramp up to today's
# 180s budget by attempt 5. Indexed by (attempts - 1); attempts is bumped
# inside _claim_pending before execution starts, so the first run is 1.
TASK_TIMEOUT_SECONDS_BY_ATTEMPT: dict[str, list[float]] = {
    "summary": [30.0, 60.0, 90.0, 120.0, 180.0],
    "embedding": [60.0, 90.0, 120.0, 180.0, 180.0],
}
RETRY_LATER_BASE_SECONDS = 2.0
RETRY_LATER_MAX_SECONDS = 16.0
HOT_INGEST_TASK_TYPES: tuple[str, ...] = ("turn_loop",)
_hot_worker_event: asyncio.Event | None = None
_hot_worker_loop: asyncio.AbstractEventLoop | None = None


class RetryTaskLater(Exception):
    """Signal that a task should be re-queued without treating it as a hard failure."""


def _timeout_for(task_type: str, attempts: int) -> float | None:
    """Return per-attempt timeout in seconds, or None if untimed.

    `attempts` is 1-indexed (the value already incremented by _claim_pending).
    Clamps past the end of the tier list to the final entry so retries past
    the array length still get a sane bound.
    """
    tiers = TASK_TIMEOUT_SECONDS_BY_ATTEMPT.get(task_type)
    if not tiers:
        return None
    idx = max(0, min(attempts - 1, len(tiers) - 1))
    return tiers[idx]


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def enqueue_ingest_tasks(db, session_id: str) -> None:
    """Insert summary + embedding tasks for session (deduped, caller commits)."""
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
    now = datetime.now(timezone.utc)
    db.add(
        SessionTask(
            session_id=session_id,
            task_type=task_type,
            max_attempts=MAX_ATTEMPTS_DEFAULT,
            created_at=now,
            updated_at=now,
        )
    )
    _notify_hot_worker(task_type)


def _is_hot_worker_lane(
    *,
    include_task_types: tuple[str, ...] | None = None,
    exclude_task_types: tuple[str, ...] | None = None,
) -> bool:
    if exclude_task_types:
        return False
    if not include_task_types:
        return False
    return tuple(include_task_types) == HOT_INGEST_TASK_TYPES


def _ensure_hot_worker_event() -> asyncio.Event:
    global _hot_worker_event
    global _hot_worker_loop

    loop = asyncio.get_running_loop()
    if _hot_worker_event is None or _hot_worker_loop is not loop:
        _hot_worker_event = asyncio.Event()
        _hot_worker_loop = loop
    return _hot_worker_event


def _notify_hot_worker(task_type: str) -> None:
    if task_type not in HOT_INGEST_TASK_TYPES:
        return
    if _hot_worker_event is None or _hot_worker_loop is None or _hot_worker_loop.is_closed():
        return
    try:
        _hot_worker_loop.call_soon_threadsafe(_hot_worker_event.set)
    except RuntimeError:
        logger.debug("Hot ingest worker notifier unavailable; falling back to polling", exc_info=True)


async def _wait_for_hot_worker_signal(*, timeout_secs: float) -> None:
    event = _ensure_hot_worker_event()
    if event.is_set():
        event.clear()
        return
    try:
        await asyncio.wait_for(event.wait(), timeout=max(timeout_secs, 0.0))
    except asyncio.TimeoutError:
        return
    finally:
        event.clear()


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


def close_current_pending_tasks(db, *, limit: int = STALE_PENDING_SWEEP_BATCH) -> int:
    """Close pending summary/embed tasks whose sessions are already current."""
    now = datetime.now(timezone.utc)
    current_tasks = (
        db.query(SessionTask)
        .join(AgentSession, AgentSession.id == SessionTask.session_id)
        .filter(SessionTask.status == "pending")
        .filter(
            or_(
                and_(
                    SessionTask.task_type == "summary",
                    AgentSession.transcript_revision > 0,
                    AgentSession.summary_revision >= AgentSession.transcript_revision,
                ),
                and_(
                    SessionTask.task_type == "embedding",
                    or_(
                        and_(
                            AgentSession.transcript_revision > 0,
                            AgentSession.embedding_revision >= AgentSession.transcript_revision,
                        ),
                        AgentSession.needs_embedding == 0,
                    ),
                ),
            )
        )
        .order_by(SessionTask.updated_at, SessionTask.created_at, SessionTask.id)
        .limit(limit)
        .all()
    )
    if not current_tasks:
        return 0

    for task in current_tasks:
        task.status = "done"
        task.error = None
        task.updated_at = now

    db.commit()
    logger.info("Closed %d stale pending ingest tasks that were already current", len(current_tasks))
    return len(current_tasks)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


async def run_ingest_task_worker(
    *,
    poll_seconds: float = WORKER_POLL_SECONDS,
    worker_name: str = "default",
    include_task_types: tuple[str, ...] | None = None,
    exclude_task_types: tuple[str, ...] | None = None,
    concurrency: int | None = None,
) -> None:
    """Background worker: poll and execute pending ingest tasks.

    Runs indefinitely. Launch as asyncio.create_task() from lifespan.

    With concurrency > 1, the worker dispatches up to N task executions in
    flight at once. A semaphore caps the live set; each completed task frees
    a slot for the next claim. Hot lane stays at concurrency=1 (serial).
    """
    hot_lane = _is_hot_worker_lane(
        include_task_types=include_task_types,
        exclude_task_types=exclude_task_types,
    )
    if concurrency is None:
        concurrency = HOT_WORKER_CONCURRENCY if hot_lane else COLD_WORKER_CONCURRENCY
    concurrency = max(1, concurrency)
    logger.info(
        "Ingest task worker started (name=%s poll=%.1fs concurrency=%d include=%s exclude=%s)",
        worker_name,
        poll_seconds,
        concurrency,
        include_task_types or (),
        exclude_task_types or (),
    )
    if hot_lane:
        _ensure_hot_worker_event()
    semaphore = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task] = set()
    while True:
        try:
            await _process_batch(
                worker_name=worker_name,
                include_task_types=include_task_types,
                exclude_task_types=exclude_task_types,
                semaphore=semaphore,
                in_flight=in_flight,
            )
        except Exception:
            logger.exception("Ingest task worker %s: unexpected error in batch", worker_name)
        if hot_lane:
            await _wait_for_hot_worker_signal(timeout_secs=poll_seconds)
        else:
            await asyncio.sleep(poll_seconds)


def _peek_has_pending(
    session_maker,
    include_task_types: tuple[str, ...] | None,
    exclude_task_types: tuple[str, ...] | None,
) -> bool:
    """Fast read-only check: any claimable pending tasks?

    Uses a plain read session (not the write serializer) so empty-queue
    polls don't pollute the task-claim metric. Accepts the sessionmaker
    directly so patched write-serializer factories work in tests.
    """
    db = session_maker()
    try:
        now = datetime.now(timezone.utc)
        q = db.query(SessionTask.id).filter(
            SessionTask.status == "pending",
            SessionTask.updated_at <= now,
        )
        if include_task_types:
            q = q.filter(SessionTask.task_type.in_(include_task_types))
        if exclude_task_types:
            q = q.filter(~SessionTask.task_type.in_(exclude_task_types))
        return q.first() is not None
    finally:
        db.close()


async def _process_batch(
    *,
    worker_name: str = "default",
    include_task_types: tuple[str, ...] | None = None,
    exclude_task_types: tuple[str, ...] | None = None,
    semaphore: asyncio.Semaphore | None = None,
    in_flight: set[asyncio.Task] | None = None,
) -> None:
    """Claim-then-dispatch loop.

    Acquire a semaphore slot, claim one task through the write serializer,
    dispatch it as a background asyncio.Task, then loop to claim more. The
    inner loop never awaits a dispatched task's result — completion just
    releases the semaphore via a done-callback. The outer worker sleep
    yields the event loop when no work is available or all slots are busy.
    """
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)
    if in_flight is None:
        in_flight = set()

    while True:
        ws = get_write_serializer()
        if not ws.is_configured:
            return
        # Block here until a slot frees. With concurrency=1 this matches the
        # old serial behavior; with concurrency=N, claims pause when full.
        await semaphore.acquire()
        try:
            # Cheap read-only peek before touching the write serializer — keeps
            # "task-claim" counts meaningful (actual claims, not idle polls).
            # Uses the serializer's own factory so patched test serializers work.
            _session_maker = ws._resolve_session_factory()
            has_work = await asyncio.to_thread(
                _peek_has_pending, _session_maker, include_task_types, exclude_task_types
            )
            if not has_work:
                semaphore.release()
                return
            tasks = await ws.execute(
                lambda db, _include=include_task_types, _exclude=exclude_task_types: _claim_pending(
                    db,
                    CLAIM_LIMIT,
                    include_task_types=_include,
                    exclude_task_types=_exclude,
                ),
                label="task-claim",
            )

            if not tasks:
                semaphore.release()
                return

            task_id, session_id, task_type, attempts = tasks[0]
        except BaseException:
            semaphore.release()
            raise

        # Dispatch as a tracked background task. Releases the semaphore + drops
        # itself from the in-flight set on completion (success, failure, retry).
        async def _runner(_id=task_id, _sid=session_id, _type=task_type, _att=attempts):
            try:
                await _execute_task(_id, _sid, _type, _att)
            except Exception:
                logger.exception("Ingest task %s (%s/%s) crashed in dispatcher", _id, _type, _sid)

        task_obj = asyncio.create_task(_runner(), name=f"ingest-task-{task_id}")
        in_flight.add(task_obj)

        def _on_done(t: asyncio.Task, _set=in_flight, _sem=semaphore):
            _set.discard(t)
            _sem.release()

        task_obj.add_done_callback(_on_done)
        # Loop: try to claim another. If the semaphore is saturated the next
        # acquire() blocks until a slot frees — fairness via in-flight cap.


def _claim_pending(
    db,
    limit: int,
    *,
    include_task_types: tuple[str, ...] | None = None,
    exclude_task_types: tuple[str, ...] | None = None,
) -> list[tuple[str, str, str, int]]:
    """Mark pending tasks as running; return (id, session_id, task_type, attempts) tuples."""
    priority = case(
        (SessionTask.task_type == "summary", 1),
        else_=2,
    )
    now = datetime.now(timezone.utc)
    pending_query = db.query(SessionTask).filter(
        SessionTask.status == "pending",
        # Skip tasks in exponential backoff (updated_at pushed into the future)
        SessionTask.updated_at <= now,
    )
    if include_task_types:
        pending_query = pending_query.filter(SessionTask.task_type.in_(include_task_types))
    if exclude_task_types:
        pending_query = pending_query.filter(~SessionTask.task_type.in_(exclude_task_types))
    # RetryLater paths bump updated_at when they yield, so claim by updated_at
    # before created_at to keep a single re-queued task from pinning the queue.
    pending = (
        pending_query.order_by(priority, SessionTask.updated_at, SessionTask.created_at, SessionTask.id)
        .limit(limit)
        .all()
    )
    if not pending:
        return []

    claimed = []
    for task in pending:
        task.status = "running"
        task.attempts = (task.attempts or 0) + 1
        task.updated_at = now
        claimed.append((task.id, task.session_id, task.task_type, task.attempts))
    # No commit — serializer auto-commits
    return claimed


async def _execute_task(task_id: str, session_id: str, task_type: str, attempts: int = 1) -> bool:
    """Execute a single task. Returns True if the task was re-queued (RetryTaskLater)."""
    ws = get_write_serializer()
    timeout_seconds = _timeout_for(task_type, attempts)
    try:
        if timeout_seconds is None:
            await _run_task_impl(task_id, session_id, task_type)
        else:
            await asyncio.wait_for(_run_task_impl(task_id, session_id, task_type), timeout=timeout_seconds)

        await ws.execute(lambda db: _mark_status(db, task_id, "done", None, False), label="task-done")
        logger.debug("Ingest task %s (%s/%s) done", task_id, task_type, session_id)
        return False
    except RetryTaskLater as e:
        logger.info("Ingest task %s (%s/%s) re-queued: %s", task_id, task_type, session_id, e)
        # RetryTaskLater means "not yet" (session still active), not a real failure.
        # Reset to pending WITHOUT consuming the retry budget so we never drop a
        # deferred ingest task just because the session was actively running.
        await ws.execute(lambda db, _e=str(e): _reset_for_retry_later(db, task_id, _e), label="task-retry")
        return True
    except asyncio.TimeoutError:
        timeout_label = f"{timeout_seconds:g}s" if timeout_seconds is not None else "unknown"
        logger.warning("Ingest task %s (%s/%s) timed out after %s", task_id, task_type, session_id, timeout_label)
        timeout_message = f"{task_type} task timed out after {timeout_label}"
        await ws.execute(
            lambda db, _msg=timeout_message: _mark_status(db, task_id, "failed", _msg, True),
            label="task-timeout",
        )
        return False
    except Exception as e:
        logger.exception("Ingest task %s (%s/%s) failed", task_id, task_type, session_id)
        await ws.execute(lambda db, _e=str(e): _mark_status(db, task_id, "failed", _e, True), label="task-fail")
        return False


async def _run_task_impl(task_id: str, session_id: str, task_type: str) -> None:
    if task_type == "summary":
        from zerg.services.session_summaries import generate_summary_impl

        await generate_summary_impl(session_id)
        return
    if task_type == "embedding":
        from zerg.services.session_summaries import generate_embeddings_impl

        await generate_embeddings_impl(session_id)
        return
    logger.warning("Unknown task_type %r for session %s", task_type, session_id)


def _reset_for_retry_later(db, task_id: str, error: str) -> None:
    """Reset a task to pending without consuming its retry budget.

    Used by RetryTaskLater — the signal means "not yet" (e.g. session is still
    actively running), not a real failure. We undo the attempt increment that
    _claim_pending applied so the retry budget is preserved for genuine errors.

    Applies exponential backoff by pushing updated_at into the future so
    _claim_pending (which orders by updated_at) naturally delays re-pickup:
    2s → 4s → 8s → 16s (capped).

    Called via WriteSerializer — no commit/rollback/close needed.
    """
    task = db.query(SessionTask).filter(SessionTask.id == task_id).first()
    if not task:
        return
    # Undo the attempt increment from _claim_pending
    task.attempts = max(0, (task.attempts or 1) - 1)
    task.retry_later_count = (task.retry_later_count or 0) + 1
    task.status = "pending"
    task.error = error[:1000] if error else None
    # Exponential backoff: 2^count * base, capped at max
    delay = min(
        RETRY_LATER_BASE_SECONDS * (2 ** (task.retry_later_count - 1)),
        RETRY_LATER_MAX_SECONDS,
    )
    task.updated_at = datetime.now(timezone.utc) + timedelta(seconds=delay)


def _mark_status(db, task_id: str, final_status: str, error: str | None, retry: bool) -> None:
    """Update task status. Called via WriteSerializer — no commit/rollback/close needed."""
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


# ---------------------------------------------------------------------------
# Failed task resurrector ("house cleaner")
# ---------------------------------------------------------------------------

RESURRECT_POLL_SECONDS = 300.0
RESURRECT_BATCH_SIZE = 100
RESURRECT_TIME_GATE_MINUTES = 30
RESURRECT_MAX_CYCLES = 5
RESURRECT_STARTUP_PACE_SECONDS = 1.0
RESURRECT_EXHAUSTED_ERROR = "exhausted retries after 5 resurrection cycles"


def _resurrect_failed_tasks_atomic(
    db,
    *,
    batch_size: int,
    time_gate_minutes: int | None,
) -> int:
    """Resurrect terminally-failed ingest tasks. MUST run inside WriteSerializer.

    Single atomic flow per call: read candidates, check active dedup against
    the same snapshot, decide outcome, write. Because this runs through the
    write serializer's lock, no concurrent enqueue can interleave between the
    "is there an active task?" check and the failed→pending flip.

    Outcomes per failed candidate:
      - session is already current for this task_type → mark done (clear error)
      - another pending/running task exists for (session, type) → leave alone
      - resurrection_count >= cap → stamp terminal error, leave failed
      - otherwise → reset to pending (attempts=0, error=None), bump cycle

    Returns the number of rows actually flipped to pending.
    """
    now = datetime.now(timezone.utc)
    q = db.query(SessionTask).filter(SessionTask.status == "failed")
    if time_gate_minutes is not None:
        cutoff = now - timedelta(minutes=time_gate_minutes)
        q = q.filter(SessionTask.updated_at < cutoff)
    candidates = (
        q.order_by(SessionTask.updated_at, SessionTask.id).limit(batch_size).all()
    )
    if not candidates:
        return 0

    resurrected = 0
    for task in candidates:
        # Active-task dedup: same (session_id, task_type) must not already have
        # a pending or running peer. Read inside the same serializer txn — any
        # concurrent enqueue is either fully visible here or fully after us.
        active_peer = (
            db.query(SessionTask.id)
            .filter(
                SessionTask.session_id == task.session_id,
                SessionTask.task_type == task.task_type,
                SessionTask.status.in_(["pending", "running"]),
                SessionTask.id != task.id,
            )
            .first()
        )
        if active_peer:
            continue

        # If session is already current, the failed work is moot — close it.
        session = (
            db.query(AgentSession).filter(AgentSession.id == task.session_id).first()
        )
        if session is not None and _session_is_current(session, task.task_type):
            task.status = "done"
            task.error = None
            task.updated_at = now
            continue

        next_cycle = (task.resurrection_count or 0) + 1
        if next_cycle > RESURRECT_MAX_CYCLES:
            # Don't churn updated_at — keeps the row sortable by oldest-first
            # and prevents us from re-touching an already-terminal row each cycle.
            if task.error != RESURRECT_EXHAUSTED_ERROR:
                task.error = RESURRECT_EXHAUSTED_ERROR
                task.updated_at = now
            continue

        task.resurrection_count = next_cycle
        task.status = "pending"
        task.attempts = 0
        task.error = None
        task.updated_at = now
        resurrected += 1

    return resurrected


def _session_is_current(session: AgentSession, task_type: str) -> bool:
    """Mirror of close_current_pending_tasks's currency rule, per task type."""
    if task_type == "summary":
        return (
            (session.transcript_revision or 0) > 0
            and (session.summary_revision or 0) >= (session.transcript_revision or 0)
        )
    if task_type == "embedding":
        if (session.needs_embedding or 0) == 0:
            return True
        return (
            (session.transcript_revision or 0) > 0
            and (session.embedding_revision or 0) >= (session.transcript_revision or 0)
        )
    return False


async def run_failed_task_resurrector(
    *,
    poll_seconds: float = RESURRECT_POLL_SECONDS,
    batch_size: int = RESURRECT_BATCH_SIZE,
) -> None:
    """Background worker: resurrect terminally-failed ingest tasks.

    First iteration: startup backfill with no time gate, paced batches so a
    big stuck pile (e.g. ~2.5k rows from a model swap) doesn't hammer the
    write serializer. Subsequent iterations: 30-minute time gate, single
    batch per cycle, 5-minute polling.
    """
    logger.info(
        "Failed-task resurrector started (poll=%.0fs batch=%d cap=%d cycles)",
        poll_seconds,
        batch_size,
        RESURRECT_MAX_CYCLES,
    )
    ws = get_write_serializer()
    # Startup backfill: drain everything failed without a time gate.
    try:
        total = 0
        while True:
            if not ws.is_configured:
                break
            count = await ws.execute(
                lambda db, _bs=batch_size: _resurrect_failed_tasks_atomic(
                    db, batch_size=_bs, time_gate_minutes=None
                ),
                label="task-resurrect",
            )
            total += count
            if count < batch_size:
                break
            await asyncio.sleep(RESURRECT_STARTUP_PACE_SECONDS)
        if total:
            logger.info("Resurrector startup backfill: resurrected %d failed tasks", total)
    except Exception:  # noqa: BLE001
        logger.exception("Resurrector startup backfill failed")

    # Steady-state loop: time-gated, single batch per cycle.
    while True:
        try:
            await asyncio.sleep(poll_seconds)
            if not ws.is_configured:
                continue
            count = await ws.execute(
                lambda db, _bs=batch_size: _resurrect_failed_tasks_atomic(
                    db, batch_size=_bs, time_gate_minutes=RESURRECT_TIME_GATE_MINUTES
                ),
                label="task-resurrect",
            )
            if count:
                logger.info("Resurrector cycle: resurrected %d failed tasks", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Resurrector cycle failed")
