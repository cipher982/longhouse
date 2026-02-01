"""SQLite-backed durable job queue.

This module provides a durable queue for scheduled jobs using SQLite.
It implements atomic claim with UPDATE ... RETURNING (SQLite 3.35+).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy.engine.url import make_url

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_LEASE_SECONDS = 300

_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


@dataclass
class QueueOwner:
    """Owner identity for queue operations."""

    name: str
    pid: int


@dataclass
class QueueJob:
    """A queued job entry."""

    id: str
    job_id: str
    scheduled_for: datetime
    attempts: int
    max_attempts: int
    status: str
    last_error: str | None = None


def _strip_quotes(value: str) -> str:
    value = (value or "").strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
    return value


def _queue_db_url() -> str:
    return _strip_quotes(os.getenv("JOB_QUEUE_DB_URL") or os.getenv("DATABASE_URL") or "")


def _queue_db_path() -> str:
    url = _queue_db_url()
    if not url:
        raise ValueError("JOB_QUEUE_DB_URL (or DATABASE_URL) is not set")

    if "://" not in url:
        path = os.path.expanduser(url)
        return os.path.abspath(path)

    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError("JOB_QUEUE_DB_URL must be a sqlite URL (e.g. sqlite:////data/sauron-queue.db)")

    path = parsed.database or ""
    if not path:
        raise ValueError("JOB_QUEUE_DB_URL missing sqlite database path")

    if path == ":memory:":
        return path

    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    return path


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def _ensure_schema(conn: sqlite3.Connection, db_path: str) -> None:
    if db_path in _SCHEMA_READY:
        return

    with _SCHEMA_LOCK:
        if db_path in _SCHEMA_READY:
            return

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_queue (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'success', 'failure', 'dead')),
                scheduled_for TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                lease_owner TEXT,
                lease_expires_at TEXT,
                dedupe_key TEXT UNIQUE,
                payload TEXT,
                last_error TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_queue_status_scheduled
            ON job_queue (status, scheduled_for)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_queue_job_id
            ON job_queue (job_id, created_at DESC)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_queue_lease_expires
            ON job_queue (lease_expires_at)
            WHERE status = 'running'
            """
        )

        _SCHEMA_READY.add(db_path)


def _connect() -> sqlite3.Connection:
    db_path = _queue_db_path()

    if db_path != ":memory:":
        dir_path = os.path.dirname(db_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    _ensure_schema(conn, db_path)
    return conn


def _dt_to_str(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="seconds")


def _str_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _owner_key(owner: QueueOwner | None) -> str | None:
    if owner is None:
        return None
    return f"{owner.name}:{owner.pid}"


def _row_to_job(row: sqlite3.Row | None) -> QueueJob | None:
    if not row:
        return None
    return QueueJob(
        id=row["id"],
        job_id=row["job_id"],
        scheduled_for=_str_to_dt(row["scheduled_for"]) or datetime.now(timezone.utc),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        status=row["status"],
        last_error=row["last_error"],
    )


def default_owner() -> QueueOwner:
    """Return default queue owner based on hostname and PID."""
    import socket

    return QueueOwner(name=socket.gethostname()[:32], pid=os.getpid())


def make_dedupe_key(job_id: str, scheduled_for: datetime) -> str:
    """Generate dedupe key for a job run."""
    return f"{job_id}:{_dt_to_str(scheduled_for)}"


def backfill_start(now: datetime) -> datetime:
    """Return the start time for backfilling missed runs."""
    return now - timedelta(hours=24)


def _run_with_conn(func, *args, **kwargs):
    conn = _connect()
    try:
        return func(conn, *args, **kwargs)
    finally:
        conn.close()


async def enqueue_job(
    job_id: str,
    scheduled_for: datetime,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
) -> str | None:
    """Enqueue a job for future execution."""
    return await asyncio.to_thread(_enqueue_job_sync, job_id, scheduled_for, dedupe_key, max_attempts)


def _enqueue_job_sync(job_id: str, scheduled_for: datetime, dedupe_key: str | None, max_attempts: int) -> str | None:
    queue_id = str(uuid.uuid4())
    scheduled_str = _dt_to_str(scheduled_for)
    now_str = _dt_to_str(datetime.now(timezone.utc))
    dedupe_key = dedupe_key or make_dedupe_key(job_id, scheduled_for)

    def _insert(conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            """
            INSERT INTO job_queue (
                id, job_id, status, scheduled_for, attempts, max_attempts,
                dedupe_key, created_at, updated_at
            ) VALUES (
                :id, :job_id, 'queued', :scheduled_for, 0, :max_attempts,
                :dedupe_key, :created_at, :updated_at
            )
            ON CONFLICT(dedupe_key) DO NOTHING
            RETURNING id
            """,
            {
                "id": queue_id,
                "job_id": job_id,
                "scheduled_for": scheduled_str,
                "max_attempts": max_attempts,
                "dedupe_key": dedupe_key,
                "created_at": now_str,
                "updated_at": now_str,
            },
        ).fetchone()
        return row["id"] if row else None

    return _run_with_conn(_insert)


async def claim_next_job(owner: QueueOwner) -> QueueJob | None:
    """Claim next available job for execution."""
    return await asyncio.to_thread(_claim_next_job_sync, owner)


def _claim_next_job_sync(owner: QueueOwner) -> QueueJob | None:
    now = datetime.now(timezone.utc)
    now_str = _dt_to_str(now)
    lease_expires = _dt_to_str(now + timedelta(seconds=DEFAULT_LEASE_SECONDS))
    owner_key = _owner_key(owner)

    def _claim(conn: sqlite3.Connection) -> QueueJob | None:
        row = conn.execute(
            """
            UPDATE job_queue
            SET status = 'running',
                attempts = attempts + 1,
                lease_owner = :lease_owner,
                lease_expires_at = :lease_expires_at,
                started_at = COALESCE(started_at, :now),
                updated_at = :now
            WHERE id = (
                SELECT id FROM job_queue
                WHERE (
                    status = 'queued' AND scheduled_for <= :now
                ) OR (
                    status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
                )
                ORDER BY scheduled_for ASC, created_at ASC
                LIMIT 1
            )
            RETURNING id, job_id, scheduled_for, attempts, max_attempts, status, last_error
            """,
            {
                "lease_owner": owner_key,
                "lease_expires_at": lease_expires,
                "now": now_str,
            },
        ).fetchone()
        return _row_to_job(row)

    return _run_with_conn(_claim)


async def claim_job_by_id(queue_id: str, owner: QueueOwner) -> QueueJob | None:
    """Claim a specific job by ID if it is available."""
    return await asyncio.to_thread(_claim_job_by_id_sync, queue_id, owner)


def _claim_job_by_id_sync(queue_id: str, owner: QueueOwner) -> QueueJob | None:
    now = datetime.now(timezone.utc)
    now_str = _dt_to_str(now)
    lease_expires = _dt_to_str(now + timedelta(seconds=DEFAULT_LEASE_SECONDS))
    owner_key = _owner_key(owner)

    def _claim(conn: sqlite3.Connection) -> QueueJob | None:
        row = conn.execute(
            """
            UPDATE job_queue
            SET status = 'running',
                attempts = attempts + 1,
                lease_owner = :lease_owner,
                lease_expires_at = :lease_expires_at,
                started_at = COALESCE(started_at, :now),
                updated_at = :now
            WHERE id = :id
              AND (
                  status = 'queued'
                  OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= :now))
              )
            RETURNING id, job_id, scheduled_for, attempts, max_attempts, status, last_error
            """,
            {
                "id": queue_id,
                "lease_owner": owner_key,
                "lease_expires_at": lease_expires,
                "now": now_str,
            },
        ).fetchone()
        return _row_to_job(row)

    return _run_with_conn(_claim)


async def complete_job(
    queue_id: str,
    status: str,
    error: str | None = None,
    owner: QueueOwner | None = None,
) -> bool:
    """Mark job as complete."""
    return await asyncio.to_thread(_complete_job_sync, queue_id, status, error, owner)


def _complete_job_sync(queue_id: str, status: str, error: str | None, owner: QueueOwner | None) -> bool:
    now_str = _dt_to_str(datetime.now(timezone.utc))
    owner_key = _owner_key(owner)

    def _complete(conn: sqlite3.Connection) -> bool:
        params = {
            "id": queue_id,
            "status": status,
            "error": error,
            "now": now_str,
        }
        if owner_key:
            params["lease_owner"] = owner_key
            query = """
                UPDATE job_queue
                SET status = :status,
                    last_error = :error,
                    finished_at = :now,
                    updated_at = :now,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = :id AND lease_owner = :lease_owner
            """
        else:
            query = """
                UPDATE job_queue
                SET status = :status,
                    last_error = :error,
                    finished_at = :now,
                    updated_at = :now,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = :id
            """
        cur = conn.execute(query, params)
        return cur.rowcount > 0

    return _run_with_conn(_complete)


async def reschedule_job(
    queue_id: str,
    retry_at: datetime,
    error: str | None = None,
    owner: QueueOwner | None = None,
) -> bool:
    """Reschedule a job for retry."""
    return await asyncio.to_thread(_reschedule_job_sync, queue_id, retry_at, error, owner)


def _reschedule_job_sync(queue_id: str, retry_at: datetime, error: str | None, owner: QueueOwner | None) -> bool:
    now_str = _dt_to_str(datetime.now(timezone.utc))
    retry_str = _dt_to_str(retry_at)
    owner_key = _owner_key(owner)

    def _reschedule(conn: sqlite3.Connection) -> bool:
        params = {
            "id": queue_id,
            "retry_at": retry_str,
            "error": error,
            "now": now_str,
        }
        if owner_key:
            params["lease_owner"] = owner_key
            query = """
                UPDATE job_queue
                SET status = 'queued',
                    scheduled_for = :retry_at,
                    last_error = :error,
                    updated_at = :now,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = :id AND lease_owner = :lease_owner
            """
        else:
            query = """
                UPDATE job_queue
                SET status = 'queued',
                    scheduled_for = :retry_at,
                    last_error = :error,
                    updated_at = :now,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = :id
            """
        cur = conn.execute(query, params)
        return cur.rowcount > 0

    return _run_with_conn(_reschedule)


async def extend_lease(
    queue_id: str,
    owner: QueueOwner,
    lease_seconds: int,
) -> bool:
    """Extend job lease."""
    return await asyncio.to_thread(_extend_lease_sync, queue_id, owner, lease_seconds)


def _extend_lease_sync(queue_id: str, owner: QueueOwner, lease_seconds: int) -> bool:
    now = datetime.now(timezone.utc)
    lease_expires = _dt_to_str(now + timedelta(seconds=lease_seconds))
    owner_key = _owner_key(owner)

    def _extend(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            """
            UPDATE job_queue
            SET lease_expires_at = :lease_expires_at,
                updated_at = :now
            WHERE id = :id AND lease_owner = :lease_owner AND status = 'running'
            """,
            {
                "lease_expires_at": lease_expires,
                "now": _dt_to_str(now),
                "id": queue_id,
                "lease_owner": owner_key,
            },
        )
        return cur.rowcount > 0

    return _run_with_conn(_extend)


async def get_last_scheduled_for(job_id: str) -> datetime | None:
    """Get last scheduled time for a job."""
    return await asyncio.to_thread(_get_last_scheduled_for_sync, job_id)


def _get_last_scheduled_for_sync(job_id: str) -> datetime | None:
    def _get(conn: sqlite3.Connection) -> datetime | None:
        row = conn.execute(
            """
            SELECT scheduled_for
            FROM job_queue
            WHERE job_id = :job_id
            ORDER BY scheduled_for DESC
            LIMIT 1
            """,
            {"job_id": job_id},
        ).fetchone()
        if not row:
            return None
        return _str_to_dt(row["scheduled_for"])

    return _run_with_conn(_get)


async def cleanup_zombies() -> int:
    """Clean up zombie jobs (stale running entries)."""
    return await asyncio.to_thread(_cleanup_zombies_sync)


def _cleanup_zombies_sync() -> int:
    now_str = _dt_to_str(datetime.now(timezone.utc))

    def _cleanup(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            """
            UPDATE job_queue
            SET status = 'queued',
                lease_owner = NULL,
                lease_expires_at = NULL,
                updated_at = :now
            WHERE status = 'running'
              AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
            """,
            {"now": now_str},
        )
        return cur.rowcount

    return _run_with_conn(_cleanup)


async def get_recent_queue_entries(limit: int = 20) -> list[dict[str, Any]]:
    """Get recent queue entries for debugging."""
    return await asyncio.to_thread(_get_recent_queue_entries_sync, limit)


def _get_recent_queue_entries_sync(limit: int) -> list[dict[str, Any]]:
    def _get(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT id, job_id, status, scheduled_for, attempts, max_attempts,
                   lease_owner, lease_expires_at, last_error, created_at, updated_at
            FROM job_queue
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"limit": limit},
        ).fetchall()
        return [dict(row) for row in rows]

    return _run_with_conn(_get)
