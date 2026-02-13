"""Session reflection service — batch analysis of recent sessions to extract insights.

Public API:
    reflect(): Analyze recent sessions for a project (or cross-project) and extract insights.
    ReflectionResult: Dataclass with results of a reflection run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.work import ReflectionRun

logger = logging.getLogger(__name__)


@dataclass
class ReflectionResult:
    """Result of a reflection run."""

    run_id: str
    project: str | None
    session_count: int = 0
    insights_created: int = 0
    insights_merged: int = 0
    insights_skipped: int = 0
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "session_count": self.session_count,
            "insights_created": self.insights_created,
            "insights_merged": self.insights_merged,
            "insights_skipped": self.insights_skipped,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "error": self.error,
        }


async def reflect(
    db: Session,
    project: str | None = None,
    window_hours: int = 24,
    llm_client: Any = None,
    model: str | None = None,
) -> ReflectionResult:
    """Analyze recent sessions and extract insights.

    Args:
        db: SQLAlchemy session.
        project: Filter to a specific project (None = all projects).
        window_hours: How far back to look for unreflected sessions.
        llm_client: Async OpenAI-compatible client for LLM calls.
        model: Model ID to use for analysis.

    Returns:
        ReflectionResult with counts of insights created/merged/skipped.
    """
    from zerg.services.reflection.collector import collect_sessions
    from zerg.services.reflection.judge import analyze_sessions
    from zerg.services.reflection.writer import execute_actions

    # Create run record
    run = ReflectionRun(project=project, window_hours=window_hours, model=model)
    db.add(run)
    db.commit()
    db.refresh(run)

    result = ReflectionResult(run_id=str(run.id), project=project, model=model)

    try:
        # Collect unreflected sessions grouped by project
        batches = collect_sessions(db, project=project, window_hours=window_hours)

        total_sessions = sum(len(b.sessions) for b in batches)
        result.session_count = total_sessions
        run.session_count = total_sessions

        if total_sessions == 0:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            db.commit()
            return result

        # Analyze each project batch with LLM
        all_actions: list[dict[str, Any]] = []
        for batch in batches:
            actions, usage = await analyze_sessions(batch, llm_client=llm_client, model=model)
            all_actions.extend(actions)
            result.prompt_tokens += usage.get("prompt_tokens", 0)
            result.completion_tokens += usage.get("completion_tokens", 0)

        result.actions = all_actions

        # Execute actions (create/merge insights, stamp sessions)
        created, merged, skipped = execute_actions(db, all_actions, batches)
        result.insights_created = created
        result.insights_merged = merged
        result.insights_skipped = skipped

        # Update run record
        run.insights_created = created
        run.insights_merged = merged
        run.insights_skipped = skipped
        run.prompt_tokens = result.prompt_tokens
        run.completion_tokens = result.completion_tokens
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        db.commit()

    except Exception as e:
        logger.exception("Reflection run %s failed", run.id)
        result.error = str(e)[:5000]
        run.status = "failed"
        run.error = str(e)[:5000]
        run.completed_at = datetime.now(UTC)
        db.commit()

    return result


__all__ = ["reflect", "ReflectionResult"]
