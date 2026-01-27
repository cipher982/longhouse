"""Publish job definitions to Life-Hub for ops dashboard."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from zerg.jobs import get_manifest_metadata, job_registry

logger = logging.getLogger(__name__)


def _life_hub_endpoint() -> str | None:
    base = os.getenv("LIFE_HUB_API_URL")
    if not base:
        return None
    return base.rstrip("/") + "/ingest/jobs/definition"


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("LIFE_HUB_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _build_definition(job: Any, scheduler_name: str) -> dict[str, Any]:
    meta = get_manifest_metadata(job.id) or {}
    metadata = {"description": job.description} if job.description else {}
    if meta:
        metadata.update(meta)

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
        "script_source": "git" if meta else "builtin",
    }
    return payload


def publish_job_definitions() -> int:
    """Publish current job definitions to Life-Hub ops.jobs."""
    endpoint = _life_hub_endpoint()
    if not endpoint:
        logger.info("LIFE_HUB_API_URL not set; skipping job definition publish")
        return 0

    scheduler_name = os.getenv("JOB_SCHEDULER_NAME", "sauron")
    headers = _headers()
    published = 0

    for job in job_registry.list_jobs():
        payload = _build_definition(job, scheduler_name)
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            published += 1
        except Exception as e:
            logger.warning("Failed to publish job definition %s: %s", job.id, e)

    logger.info("Published %d job definitions to Life-Hub", published)
    return published
