"""Direct database client for job queue operations.

Fire-and-forget pattern: jobs continue even if DB is unreachable.
Writes directly to the ops.* tables for run history.

Uses the main DATABASE_URL (single Postgres source of truth).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Connection pool (lazy init)
_pool: Any = None
_runs_columns: list[str] | None = None


def get_job_queue_db_url() -> str | None:
    """Get the database URL for the job queue (uses DATABASE_URL)."""
    from zerg.config import get_settings

    return get_settings().database_url or None


def is_job_queue_db_enabled() -> bool:
    """Feature flag: enabled only if job queue is on and DB URL is configured."""
    from zerg.config import get_settings

    settings = get_settings()
    return settings.job_queue_enabled and bool(settings.database_url)


def _get_emit_timeout_seconds() -> int:
    """Get overall emit timeout from environment."""
    return int(os.getenv("JOB_EMIT_TIMEOUT_SECONDS", "30"))


def _job_key(job_id: str, scheduler: str = "zerg") -> str:
    """Generate a stable job key for ops.runs queries."""
    return f"{scheduler}:{job_id}"


def _definition_hash(
    *,
    job_key: str,
    job_id: str,
    scheduler: str,
    project: str | None,
    cron: str,
    timezone: str,
    enabled: bool,
    timeout_seconds: int | None,
    tags: list[str] | None,
    source_host: str,
) -> str:
    """Generate a hash of job definition for change detection."""
    payload = {
        "job_key": job_key,
        "job_id": job_id,
        "scheduler": scheduler,
        "project": project,
        "cron": cron,
        "timezone": timezone,
        "enabled": enabled,
        "timeout_seconds": timeout_seconds,
        "tags": sorted(set(tags or [])),
        "source_host": source_host,
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def get_pool():
    """Get or create connection pool."""
    global _pool
    if _pool is None:
        import asyncpg

        db_url = get_job_queue_db_url()
        if not db_url:
            raise RuntimeError("DATABASE_URL not configured")
        _pool = await asyncpg.create_pool(
            db_url,
            min_size=1,
            max_size=3,
            command_timeout=10,
        )
    return _pool


async def _get_runs_columns(conn) -> list[str]:
    """Fetch and cache ops.runs columns for schema compatibility."""
    global _runs_columns
    if _runs_columns is not None:
        return _runs_columns

    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'ops'
          AND table_name = 'runs'
        ORDER BY ordinal_position
        """
    )
    _runs_columns = [row["column_name"] for row in rows]
    return _runs_columns


async def emit_job_run(
    job_id: str,
    status: str,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    duration_ms: int | None = None,
    exit_code: int | None = None,
    error_message: str | None = None,
    error_type: str | None = None,
    summary: str | None = None,
    stdout_tail: str | None = None,
    tags: list[str] | None = None,
    project: str | None = None,
    metadata: dict[str, Any] | None = None,
    job_key: str | None = None,
    scheduler: str = "zerg",
) -> bool:
    """Emit job run status to ops.runs table.

    Fire-and-forget: logs on failure, never raises.

    Natural key is (job_key, started_at) - idempotent for retries.
    """
    if not is_job_queue_db_enabled():
        raise RuntimeError("emit_job_run called but job queue is disabled or DATABASE_URL is missing.")

    try:
        pool = await get_pool()
        resolved_job_key = job_key or _job_key(job_id, scheduler)

        # Prepare metadata as JSON string
        metadata_json = json.dumps(metadata) if metadata else None

        async with pool.acquire() as conn:
            available = await _get_runs_columns(conn)

            values: dict[str, Any] = {
                "job_key": resolved_job_key,
                "job_id": job_id,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "status": status,
                "exit_code": exit_code,
                "error_message": error_message[:5000] if error_message else None,
                "error_type": error_type,
                "summary": summary,
                "stdout_tail": stdout_tail[:5000] if stdout_tail else None,
                "tags": tags or [],
                "project": project,
                "source_host": scheduler,
                "metadata": metadata_json,
            }

            columns = [col for col in values.keys() if col in available]
            if not columns:
                logger.error("ops.runs has no compatible columns; skipping emit")
                return False

            params = []
            for idx, col in enumerate(columns, start=1):
                if col == "metadata":
                    params.append(f"${idx}::jsonb")
                else:
                    params.append(f"${idx}")

            conflict_target = None
            if "job_key" in columns and "started_at" in columns:
                conflict_target = "(job_key, started_at)"
            elif "job_id" in columns and "started_at" in columns:
                conflict_target = "(job_id, started_at)"

            on_conflict = f"ON CONFLICT {conflict_target} DO NOTHING" if conflict_target else ""

            sql = f"INSERT INTO ops.runs ({', '.join(columns)}) VALUES ({', '.join(params)}) {on_conflict}"

            params_values = [values[col] for col in columns]
            try:
                await conn.execute(sql, *params_values)
            except Exception as e:
                message = str(e)
                if on_conflict and "no unique or exclusion constraint" in message.lower():
                    sql_no_conflict = f"INSERT INTO ops.runs ({', '.join(columns)}) VALUES ({', '.join(params)})"
                    await conn.execute(sql_no_conflict, *params_values)
                else:
                    raise

        logger.info(f"Emitted job run: {resolved_job_key} ({status})")
        return True

    except Exception as e:
        logger.error(f"Failed to emit job run: {e}")
        return False


async def emit_job_run_fire_and_forget(
    job_id: str,
    status: str,
    **kwargs: Any,
) -> None:
    """Emit job run status without blocking.

    Spawns a background task. The calling job continues immediately.
    """
    asyncio.create_task(
        _emit_with_timeout(job_id, status, **kwargs),
        name=f"emit-{job_id}",
    )


async def emit_job_run_with_timeout(
    job_id: str,
    status: str,
    **kwargs: Any,
) -> None:
    """Emit job run status and wait for completion (with timeout)."""
    await _emit_with_timeout(job_id, status, **kwargs)


async def _emit_with_timeout(job_id: str, status: str, **kwargs: Any) -> None:
    """Emit with overall timeout to prevent hanging forever."""
    timeout_seconds = _get_emit_timeout_seconds()
    try:
        await asyncio.wait_for(
            emit_job_run(job_id, status, **kwargs),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.error(f"Job run emit timed out after {timeout_seconds}s: {job_id}")
    except Exception as e:
        logger.exception(f"Job run emit failed: {e}")


async def close_pool() -> None:
    """Close connection pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def get_job_definition(job_id: str, scheduler: str = "zerg") -> dict | None:
    """Load job definition from ops.jobs table.

    Returns dict with job definition fields, or None if not found.
    """
    if not is_job_queue_db_enabled():
        return None

    try:
        pool = await get_pool()
        job_key = _job_key(job_id, scheduler)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT job_id, cron, enabled, timeout_seconds, tags, project,
                       script_source, entrypoint, config
                FROM ops.jobs
                WHERE job_key = $1
                """,
                job_key,
            )

            if not row:
                return None

            return {
                "job_id": row["job_id"],
                "cron": row["cron"],
                "enabled": row["enabled"],
                "timeout_seconds": row["timeout_seconds"],
                "tags": row["tags"] or [],
                "project": row["project"],
                "script_source": row["script_source"] or "builtin",
                "entrypoint": row["entrypoint"],
                "config": json.loads(row["config"]) if row["config"] else {},
            }

    except Exception as e:
        logger.error(f"Failed to get job definition: {e}")
        return None


async def list_job_definitions(scheduler: str = "zerg", enabled_only: bool = True) -> list[dict]:
    """Load all job definitions from ops.jobs table.

    Returns list of job definition dicts.
    """
    if not is_job_queue_db_enabled():
        return []

    try:
        pool = await get_pool()

        async with pool.acquire() as conn:
            query = """
                SELECT job_id, cron, enabled, timeout_seconds, tags, project,
                       script_source, entrypoint, config
                FROM ops.jobs
                WHERE scheduler = $1
            """
            if enabled_only:
                query += " AND enabled = true"

            rows = await conn.fetch(query, scheduler)

            return [
                {
                    "job_id": row["job_id"],
                    "cron": row["cron"],
                    "enabled": row["enabled"],
                    "timeout_seconds": row["timeout_seconds"],
                    "tags": row["tags"] or [],
                    "project": row["project"],
                    "script_source": row["script_source"] or "builtin",
                    "entrypoint": row["entrypoint"],
                    "config": json.loads(row["config"]) if row["config"] else {},
                }
                for row in rows
            ]

    except Exception as e:
        logger.error(f"Failed to list job definitions: {e}")
        return []
