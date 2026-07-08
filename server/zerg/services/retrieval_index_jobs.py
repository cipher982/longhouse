"""Durable background jobs for rebuilding the recall retrieval index."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_session_factory
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.internal_sessions import internal_canary_session_clause
from zerg.services.internal_sessions import is_internal_canary_provider_filter
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.retrieval_index import child_chunk_count
from zerg.services.retrieval_index import connect_retrieval_db
from zerg.services.retrieval_index import initialize_retrieval_db
from zerg.services.retrieval_index import project_session_chunks
from zerg.services.retrieval_index import replace_session_chunks
from zerg.services.retrieval_index import resolve_retrieval_db_path

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("done", "error", "canceled")
DEFAULT_TENANT = "single"
STALE_RUNNING_SECONDS = 300

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event | None = None


@dataclass(frozen=True)
class RecallIndexJob:
    id: str
    tenant: str
    status: str
    project: str | None
    provider: str | None
    since_days: int
    limit_count: int
    progress_total: int
    progress_done: int
    sessions_indexed: int
    chunks_indexed: int
    child_chunk_count: int
    cancel_requested: bool
    heartbeat_at: str | None
    error: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant": self.tenant,
            "status": self.status,
            "project": self.project,
            "provider": self.provider,
            "since_days": self.since_days,
            "limit": self.limit_count,
            "progress_total": self.progress_total,
            "progress_done": self.progress_done,
            "sessions_indexed": self.sessions_indexed,
            "chunks_indexed": self.chunks_indexed,
            "child_chunk_count": self.child_chunk_count,
            "cancel_requested": self.cancel_requested,
            "heartbeat_at": self.heartbeat_at,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_job(row: sqlite3.Row | None) -> RecallIndexJob | None:
    if row is None:
        return None
    return RecallIndexJob(
        id=str(row["id"]),
        tenant=str(row["tenant"]),
        status=str(row["status"]),
        project=row["project"],
        provider=row["provider"],
        since_days=int(row["since_days"]),
        limit_count=int(row["limit_count"]),
        progress_total=int(row["progress_total"] or 0),
        progress_done=int(row["progress_done"] or 0),
        sessions_indexed=int(row["sessions_indexed"] or 0),
        chunks_indexed=int(row["chunks_indexed"] or 0),
        child_chunk_count=int(row["child_chunk_count"] or 0),
        cancel_requested=bool(row["cancel_requested"]),
        heartbeat_at=row["heartbeat_at"],
        error=row["error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def get_recall_index_job(conn: sqlite3.Connection, job_id: str) -> RecallIndexJob | None:
    row = conn.execute("SELECT * FROM recall_index_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def get_active_recall_index_job(conn: sqlite3.Connection, *, tenant: str = DEFAULT_TENANT) -> RecallIndexJob | None:
    row = conn.execute(
        """
        SELECT *
        FROM recall_index_jobs
        WHERE tenant = ? AND status IN ('queued', 'running')
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (tenant,),
    ).fetchone()
    return _row_to_job(row)


def get_latest_recall_index_job(conn: sqlite3.Connection, *, tenant: str = DEFAULT_TENANT) -> RecallIndexJob | None:
    row = conn.execute(
        """
        SELECT *
        FROM recall_index_jobs
        WHERE tenant = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tenant,),
    ).fetchone()
    return _row_to_job(row)


def enqueue_recall_index_job(
    conn: sqlite3.Connection,
    *,
    project: str | None,
    provider: str | None,
    since_days: int,
    limit: int,
    tenant: str = DEFAULT_TENANT,
) -> tuple[RecallIndexJob, bool]:
    """Create one recall index job, or return the active job already running."""

    now = _utc_now_iso()
    job_id = str(uuid4())
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO recall_index_jobs(
                  id, tenant, status, project, provider, since_days, limit_count,
                  created_at, updated_at
                )
                VALUES(?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (job_id, tenant, project, provider, since_days, limit, now, now),
            )
    except sqlite3.IntegrityError:
        active = get_active_recall_index_job(conn, tenant=tenant)
        if active is None:
            raise
        return active, False
    job = get_recall_index_job(conn, job_id)
    if job is None:  # pragma: no cover - defensive
        raise RuntimeError(f"recall index job {job_id} disappeared after enqueue")
    return job, True


def request_recall_index_cancel(conn: sqlite3.Connection, job_id: str) -> RecallIndexJob | None:
    now = _utc_now_iso()
    with conn:
        conn.execute(
            """
            UPDATE recall_index_jobs
            SET cancel_requested = 1, updated_at = ?
            WHERE id = ? AND status IN ('queued', 'running')
            """,
            (now, job_id),
        )
    return get_recall_index_job(conn, job_id)


def requeue_stale_recall_index_jobs(conn: sqlite3.Connection, *, stale_after_seconds: int = STALE_RUNNING_SECONDS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
    now = _utc_now_iso()
    with conn:
        cursor = conn.execute(
            """
            UPDATE recall_index_jobs
            SET status = 'queued',
                heartbeat_at = NULL,
                updated_at = ?,
                error = NULL
            WHERE status = 'running'
              AND (heartbeat_at IS NULL OR heartbeat_at < ?)
            """,
            (now, cutoff),
        )
    return int(cursor.rowcount or 0)


def recall_index_jobs_table_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'recall_index_jobs'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def claim_next_recall_index_job(conn: sqlite3.Connection, *, tenant: str = DEFAULT_TENANT) -> RecallIndexJob | None:
    now = _utc_now_iso()
    transaction_open = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        transaction_open = True
        row = conn.execute(
            """
            SELECT id
            FROM recall_index_jobs
            WHERE tenant = ? AND status = 'queued'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (tenant,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            transaction_open = False
            return None
        job_id = str(row["id"])
        conn.execute(
            """
            UPDATE recall_index_jobs
            SET status = 'running',
                heartbeat_at = ?,
                started_at = COALESCE(started_at, ?),
                updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, now, job_id),
        )
        conn.execute("COMMIT")
        transaction_open = False
    except BaseException:
        if transaction_open:
            conn.execute("ROLLBACK")
        raise
    return get_recall_index_job(conn, job_id)


def _update_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    progress_total: int | None = None,
    progress_done: int | None = None,
    sessions_indexed: int | None = None,
    chunks_indexed: int | None = None,
    child_chunks: int | None = None,
    heartbeat: bool = True,
    error: str | None = None,
    finished: bool = False,
) -> None:
    now = _utc_now_iso()
    with conn:
        cursor = conn.execute(
            """
            UPDATE recall_index_jobs
            SET status = COALESCE(?, status),
                progress_total = COALESCE(?, progress_total),
                progress_done = COALESCE(?, progress_done),
                sessions_indexed = COALESCE(?, sessions_indexed),
                chunks_indexed = COALESCE(?, chunks_indexed),
                child_chunk_count = COALESCE(?, child_chunk_count),
                heartbeat_at = CASE WHEN ? THEN ? ELSE heartbeat_at END,
                error = ?,
                updated_at = ?,
                finished_at = CASE WHEN ? THEN ? ELSE finished_at END
            WHERE id = ?
            """,
            (
                status,
                progress_total,
                progress_done,
                sessions_indexed,
                chunks_indexed,
                child_chunks,
                1 if heartbeat else 0,
                now,
                error,
                now,
                1 if finished else 0,
                now,
                job_id,
            ),
        )
    if cursor.rowcount == 0:
        raise RuntimeError(f"recall index job {job_id} not found")


def _candidate_sessions(db: Session, job: RecallIndexJob) -> list[AgentSession]:
    since = datetime.now(timezone.utc) - timedelta(days=job.since_days)
    session_query = db.query(AgentSession).filter((AgentSession.started_at >= since) | (AgentSession.last_activity_at >= since))
    if job.project:
        session_query = session_query.filter(AgentSession.project == job.project)
    if job.provider:
        session_query = session_query.filter(AgentSession.provider == job.provider)
    if not is_internal_canary_provider_filter(job.provider):
        session_query = session_query.filter(~internal_canary_session_clause(AgentSession))
    session_query = session_query.filter(AgentSession.user_messages > 0)
    return session_query.order_by(AgentSession.last_activity_at.desc(), AgentSession.started_at.desc()).limit(job.limit_count).all()


def _index_one_job(
    *,
    retrieval_path: Path,
    session_factory: Any,
    tenant: str = DEFAULT_TENANT,
) -> RecallIndexJob | None:
    with connect_retrieval_db(retrieval_path) as retrieval_db:
        if not recall_index_jobs_table_ready(retrieval_db):
            return None
        requeue_stale_recall_index_jobs(retrieval_db)
        job = claim_next_recall_index_job(retrieval_db, tenant=tenant)
        if job is None:
            return None
        initialize_retrieval_db(retrieval_db)

        sessions_indexed = job.sessions_indexed
        chunks_indexed = job.chunks_indexed
        try:
            db = session_factory()
            try:
                sessions = _candidate_sessions(db, job)
                db.expunge_all()
                progress_total = len(sessions)
                _update_job(
                    retrieval_db,
                    job.id,
                    progress_total=progress_total,
                    progress_done=min(job.progress_done, progress_total),
                )
                latest = get_recall_index_job(retrieval_db, job.id)
                if latest is not None and latest.cancel_requested:
                    child_chunks = child_chunk_count(retrieval_db)
                    _update_job(
                        retrieval_db,
                        job.id,
                        status="canceled",
                        child_chunks=child_chunks,
                        finished=True,
                    )
                    return get_recall_index_job(retrieval_db, job.id)
                for position, session in enumerate(sessions, start=1):
                    latest = get_recall_index_job(retrieval_db, job.id)
                    if latest is not None and latest.cancel_requested:
                        child_chunks = child_chunk_count(retrieval_db)
                        _update_job(
                            retrieval_db,
                            job.id,
                            status="canceled",
                            child_chunks=child_chunks,
                            finished=True,
                        )
                        return get_recall_index_job(retrieval_db, job.id)
                    if position <= job.progress_done:
                        continue

                    events = (
                        db.query(AgentEvent)
                        .filter(AgentEvent.session_id == session.id)
                        .filter(durable_transcript_event_predicate())
                        .order_by(AgentEvent.timestamp, AgentEvent.id)
                        .all()
                    )
                    chunks = project_session_chunks(session, events)
                    if chunks:
                        chunks_indexed += replace_session_chunks(retrieval_db, str(session.id), chunks)
                        sessions_indexed += 1
                    db.expunge_all()
                    child_chunks = child_chunk_count(retrieval_db)
                    _update_job(
                        retrieval_db,
                        job.id,
                        progress_total=progress_total,
                        progress_done=position,
                        sessions_indexed=sessions_indexed,
                        chunks_indexed=chunks_indexed,
                        child_chunks=child_chunks,
                    )
            finally:
                db.close()

            child_chunks = child_chunk_count(retrieval_db)
            _update_job(
                retrieval_db,
                job.id,
                status="done",
                progress_total=progress_total,
                progress_done=progress_total,
                sessions_indexed=sessions_indexed,
                chunks_indexed=chunks_indexed,
                child_chunks=child_chunks,
                finished=True,
            )
            return get_recall_index_job(retrieval_db, job.id)
        except BaseException as exc:
            logger.exception("Recall index job %s failed", job.id)
            _update_job(
                retrieval_db,
                job.id,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                finished=True,
            )
            return get_recall_index_job(retrieval_db, job.id)


def run_recall_index_job_once(
    *,
    database_url: str | None = None,
    session_factory: Any | None = None,
    tenant: str = DEFAULT_TENANT,
) -> RecallIndexJob | None:
    """Run at most one queued recall index job synchronously."""

    resolved_database_url = database_url or get_settings().database_url
    retrieval_path = resolve_retrieval_db_path(resolved_database_url)
    if retrieval_path is None or not retrieval_path.exists():
        return None
    return _index_one_job(
        retrieval_path=retrieval_path,
        session_factory=session_factory or get_session_factory(),
        tenant=tenant,
    )


async def _worker_loop() -> None:
    assert _wake_event is not None
    while True:
        try:
            job = await asyncio.to_thread(run_recall_index_job_once)
            if job is None:
                try:
                    await asyncio.wait_for(_wake_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                _wake_event.clear()
            else:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recall index worker tick failed")
            await asyncio.sleep(5)


def start_recall_index_worker() -> None:
    """Start the singleton in-process recall index worker."""

    global _wake_event, _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _wake_event = asyncio.Event()
    _worker_task = asyncio.create_task(_worker_loop(), name="recall-index-worker")
    logger.info("Recall index worker started")


def wake_recall_index_worker() -> None:
    if _wake_event is not None:
        _wake_event.set()


async def stop_recall_index_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
