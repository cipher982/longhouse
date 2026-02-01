"""Publish job definitions to Life-Hub for ops dashboard.

In SQLite-only mode, this module is a no-op since there's no external
ops database to publish to. When DATABASE_URL points to PostgreSQL,
it publishes job definitions for the ops dashboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

from sqlalchemy import text

from zerg.jobs import get_manifest_metadata
from zerg.jobs import job_registry

logger = logging.getLogger(__name__)


def _database_url() -> str | None:
    return os.getenv("DATABASE_URL") or os.getenv("LIFE_HUB_DB_URL")


def _is_postgres_url(url: str) -> bool:
    """Check if URL is a PostgreSQL connection string."""
    return url.startswith("postgresql://") or url.startswith("postgres://")


def _build_definition(job: Any, scheduler_name: str) -> dict[str, Any]:
    meta = get_manifest_metadata(job.id) or {}
    metadata = {"description": job.description} if job.description else {}
    if meta:
        metadata.update(meta)

    entrypoint = f"{job.func.__module__}.{job.func.__name__}"
    # Valid values: builtin, git, http (check constraint on ops.jobs)
    VALID_SOURCES = ("builtin", "git", "http")
    raw_source = meta.get("script_source", "git") if meta else "builtin"
    if raw_source not in VALID_SOURCES:
        logger.warning("Invalid script_source '%s' for job %s, coercing to 'git'", raw_source, job.id)
        script_source = "git"
    else:
        script_source = raw_source

    payload: dict[str, Any] = {
        "job_key": f"{scheduler_name}:{job.id}",
        "job_id": job.id,
        "scheduler": scheduler_name,
        "project": job.project,
        "cron": job.cron,
        "timezone": "UTC",
        "enabled": job.enabled,
        "timeout_seconds": job.timeout_seconds,
        "tags": job.tags,
        "source_host": scheduler_name,
        "metadata": metadata or None,
        "script_source": script_source,
        "entrypoint": entrypoint,
        "config": None,
    }
    return payload


def _definition_hash(defn: dict[str, Any]) -> str:
    payload = {
        "job_key": defn.get("job_key"),
        "job_id": defn.get("job_id"),
        "scheduler": defn.get("scheduler"),
        "project": defn.get("project"),
        "cron": defn.get("cron"),
        "timezone": defn.get("timezone"),
        "enabled": defn.get("enabled"),
        "timeout_seconds": defn.get("timeout_seconds"),
        "tags": sorted(set(defn.get("tags") or [])),
        "source_host": defn.get("source_host"),
        "script_source": defn.get("script_source"),
        "entrypoint": defn.get("entrypoint"),
        "config": defn.get("config"),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _publish_job_definitions_sync() -> int:
    """Synchronous implementation using SQLAlchemy."""
    db_url = _database_url()
    if not db_url:
        logger.info("DATABASE_URL not set; skipping job definition publish")
        return 0

    if not _is_postgres_url(db_url):
        logger.info("SQLite mode; skipping job definition publish (requires PostgreSQL)")
        return 0

    scheduler_name = os.getenv("JOB_SCHEDULER_NAME", "sauron")
    published = 0

    # Lazy import to avoid pulling in models.json dependency when not needed
    from zerg.database import make_engine
    from zerg.database import make_sessionmaker

    # Create a SQLAlchemy engine for the ops database
    engine = make_engine(db_url)
    SessionLocal = make_sessionmaker(engine)

    with SessionLocal() as session:
        for job in job_registry.list_jobs():
            payload = _build_definition(job, scheduler_name)
            definition_hash = _definition_hash(payload)
            metadata_json = json.dumps(payload.get("metadata")) if payload.get("metadata") else None
            config_json = json.dumps(payload.get("config")) if payload.get("config") else None

            # Use SQLAlchemy text() for raw SQL with PostgreSQL-specific features
            session.execute(
                text("""
                INSERT INTO ops.jobs (
                    job_key, job_id, scheduler, project, cron, timezone,
                    enabled, timeout_seconds, tags, source_host,
                    next_run_at, last_seen_at, definition_hash, metadata,
                    script_source, entrypoint, config
                )
                VALUES (:job_key, :job_id, :scheduler, :project, :cron, :timezone,
                        :enabled, :timeout_seconds, :tags, :source_host,
                        :next_run_at, NOW(), :definition_hash, :metadata,
                        :script_source, :entrypoint, :config)
                ON CONFLICT (job_key) DO UPDATE SET
                    job_id = EXCLUDED.job_id,
                    scheduler = EXCLUDED.scheduler,
                    project = EXCLUDED.project,
                    cron = EXCLUDED.cron,
                    timezone = EXCLUDED.timezone,
                    enabled = EXCLUDED.enabled,
                    timeout_seconds = EXCLUDED.timeout_seconds,
                    tags = EXCLUDED.tags,
                    source_host = EXCLUDED.source_host,
                    next_run_at = EXCLUDED.next_run_at,
                    last_seen_at = NOW(),
                    definition_hash = EXCLUDED.definition_hash,
                    metadata = EXCLUDED.metadata,
                    script_source = EXCLUDED.script_source,
                    entrypoint = EXCLUDED.entrypoint,
                    config = EXCLUDED.config,
                    updated_at = NOW()
                """),
                {
                    "job_key": payload.get("job_key"),
                    "job_id": payload.get("job_id"),
                    "scheduler": payload.get("scheduler"),
                    "project": payload.get("project"),
                    "cron": payload.get("cron"),
                    "timezone": payload.get("timezone"),
                    "enabled": payload.get("enabled"),
                    "timeout_seconds": payload.get("timeout_seconds"),
                    "tags": payload.get("tags") or [],
                    "source_host": payload.get("source_host"),
                    "next_run_at": payload.get("next_run_at"),
                    "definition_hash": definition_hash,
                    "metadata": metadata_json,
                    "script_source": payload.get("script_source"),
                    "entrypoint": payload.get("entrypoint"),
                    "config": config_json,
                },
            )

            session.execute(
                text("""
                INSERT INTO ops.job_definitions (
                    job_key, definition_hash,
                    job_id, scheduler, project, cron, timezone,
                    enabled, timeout_seconds, tags, source_host, metadata
                )
                VALUES (:job_key, :definition_hash, :job_id, :scheduler, :project,
                        :cron, :timezone, :enabled, :timeout_seconds, :tags,
                        :source_host, :metadata)
                ON CONFLICT (job_key, definition_hash) DO NOTHING
                """),
                {
                    "job_key": payload.get("job_key"),
                    "definition_hash": definition_hash,
                    "job_id": payload.get("job_id"),
                    "scheduler": payload.get("scheduler"),
                    "project": payload.get("project"),
                    "cron": payload.get("cron"),
                    "timezone": payload.get("timezone"),
                    "enabled": payload.get("enabled"),
                    "timeout_seconds": payload.get("timeout_seconds"),
                    "tags": payload.get("tags") or [],
                    "source_host": payload.get("source_host"),
                    "metadata": metadata_json,
                },
            )

            published += 1

        session.commit()

    engine.dispose()
    logger.info("Published %d job definitions to Life-Hub", published)
    return published


async def _publish_job_definitions() -> int:
    """Async wrapper for compatibility with existing callers."""
    return await asyncio.to_thread(_publish_job_definitions_sync)


def publish_job_definitions() -> int:
    """Publish current job definitions to Life-Hub ops.jobs."""
    return _publish_job_definitions_sync()
