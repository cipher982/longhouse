"""Scheduled reflection job — analyze recent sessions and extract insights.

Thin wrapper around the reflection service. Runs every 6 hours by default
(configurable via REFLECTION_CRON env var).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.models_config import get_llm_client_for_use_case

logger = logging.getLogger(__name__)


async def run() -> dict[str, Any]:
    """Run reflection over recent unreflected sessions."""
    from zerg.services.reflection import reflect

    try:
        client, model_id, _provider = get_llm_client_for_use_case("reflection")
    except (ValueError, KeyError):
        # Fallback: try summarization use case if reflection not configured
        try:
            client, model_id, _provider = get_llm_client_for_use_case("summarization")
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
        enabled=True,
        timeout_seconds=300,
        tags=["reflection", "insights", "builtin"],
        description="Analyze recent sessions and extract learnings",
    )
)
