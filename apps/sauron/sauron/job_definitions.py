"""Publish job definitions to Life-Hub for ops dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

import asyncpg

from zerg.jobs import get_manifest_metadata, job_registry

logger = logging.getLogger(__name__)


def _database_url() -> str | None:
    return os.getenv("DATABASE_URL") or os.getenv("LIFE_HUB_DB_URL")


def _build_definition(job: Any, scheduler_name: str) -> dict[str, Any]:
    meta = get_manifest_metadata(job.id) or {}
    metadata = {"description": job.description} if job.description else {}
    if meta:
        metadata.update(meta)

    entrypoint = f"{job.func.__module__}.{job.func.__name__}"
    # Valid values: builtin, git, http (check constraint on ops.jobs)
    raw_source = meta.get("script_source", "git") if meta else "builtin"
    script_source = raw_source if raw_source in ("builtin", "git", "http") else "git"

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


async def _publish_job_definitions() -> int:
    db_url = _database_url()
    if not db_url:
        logger.info("DATABASE_URL not set; skipping job definition publish")
        return 0

    scheduler_name = os.getenv("JOB_SCHEDULER_NAME", "sauron")
    published = 0

    conn = await asyncpg.connect(db_url)
    try:
        for job in job_registry.list_jobs():
            payload = _build_definition(job, scheduler_name)
            definition_hash = _definition_hash(payload)
            metadata_json = json.dumps(payload.get("metadata")) if payload.get("metadata") else None
            config_json = json.dumps(payload.get("config")) if payload.get("config") else None

            await conn.execute(
                """
                INSERT INTO ops.jobs (
                    job_key, job_id, scheduler, project, cron, timezone,
                    enabled, timeout_seconds, tags, source_host,
                    next_run_at, last_seen_at, definition_hash, metadata,
                    script_source, entrypoint, config
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), $12, $13, $14, $15, $16)
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
                """,
                payload.get("job_key"),
                payload.get("job_id"),
                payload.get("scheduler"),
                payload.get("project"),
                payload.get("cron"),
                payload.get("timezone"),
                payload.get("enabled"),
                payload.get("timeout_seconds"),
                payload.get("tags") or [],
                payload.get("source_host"),
                payload.get("next_run_at"),
                definition_hash,
                metadata_json,
                payload.get("script_source"),
                payload.get("entrypoint"),
                config_json,
            )

            await conn.execute(
                """
                INSERT INTO ops.job_definitions (
                    job_key, definition_hash,
                    job_id, scheduler, project, cron, timezone,
                    enabled, timeout_seconds, tags, source_host, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (job_key, definition_hash) DO NOTHING
                """,
                payload.get("job_key"),
                definition_hash,
                payload.get("job_id"),
                payload.get("scheduler"),
                payload.get("project"),
                payload.get("cron"),
                payload.get("timezone"),
                payload.get("enabled"),
                payload.get("timeout_seconds"),
                payload.get("tags") or [],
                payload.get("source_host"),
                metadata_json,
            )

            published += 1
    finally:
        await conn.close()

    logger.info("Published %d job definitions to Life-Hub", published)
    return published


def publish_job_definitions() -> int:
    """Publish current job definitions to Life-Hub ops.jobs."""
    return asyncio.run(_publish_job_definitions())
