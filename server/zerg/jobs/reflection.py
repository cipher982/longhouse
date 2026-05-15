"""Scheduled reflection job — analyze recent sessions and extract insights.

Thin wrapper around the reflection service. The cron registration is disabled
by default and can be opted back in with ``REFLECTION_JOB_ENABLED=1``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.models_config import get_llm_client_preferring_db_config

logger = logging.getLogger(__name__)


def _reflection_job_enabled() -> bool:
    return os.getenv("REFLECTION_JOB_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


async def run() -> dict[str, Any]:
    """Run reflection over recent unreflected sessions."""
    from zerg.services.reflection import reflect

    with db_session() as config_db:
        try:
            client, model_id, _provider = get_llm_client_preferring_db_config("reflection", db=config_db)
        except (ValueError, KeyError):
            try:
                client, model_id, _provider = get_llm_client_preferring_db_config("summarization", db=config_db)
            except (ValueError, KeyError):
                logger.error("No LLM configured for reflection or summarization")
                return {"success": False, "error": "No LLM configured"}

    with db_session() as db:
        result = await reflect(
            db=db,
            window_hours=24,
            llm_client=client,
            model=model_id,
        )

    return {"success": result.error is None, **result.to_dict()}


# Register the job
job_registry.register(
    JobConfig(
        id="session-reflection",
        cron=os.getenv("REFLECTION_CRON", "0 */6 * * *"),
        func=run,
        enabled=_reflection_job_enabled(),
        timeout_seconds=300,
        tags=["reflection", "insights", "builtin"],
        description="Analyze recent sessions and extract learnings",
    )
)
