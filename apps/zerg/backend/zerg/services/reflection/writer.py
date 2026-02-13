"""Execute reflection actions — create insights, merge observations, stamp sessions.

Works with the existing insight dedup logic in the insights router/model.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from zerg.models.agents import AgentSession
from zerg.models.work import INSIGHT_DEDUP_WINDOW_DAYS
from zerg.models.work import Insight
from zerg.services.reflection.collector import ProjectBatch

logger = logging.getLogger(__name__)


def execute_actions(
    db: Session,
    actions: list[dict],
    batches: list[ProjectBatch],
) -> tuple[int, int, int]:
    """Execute reflection actions and stamp processed sessions.

    Args:
        db: SQLAlchemy session.
        actions: List of action dicts from the judge.
        batches: The original project batches (for session ID access).

    Returns:
        Tuple of (created, merged, skipped) counts.
    """
    created = 0
    merged = 0
    skipped = 0

    for action in actions:
        action_type = action.get("action")
        try:
            if action_type == "create_insight":
                if _create_insight(db, action):
                    created += 1
                else:
                    merged += 1  # dedup matched — counts as merge
            elif action_type == "merge":
                if _merge_insight(db, action):
                    merged += 1
                else:
                    skipped += 1
            elif action_type == "skip":
                skipped += 1
        except Exception:
            logger.exception("Failed to execute reflection action: %s", action)
            skipped += 1

    # Stamp all processed sessions
    _stamp_sessions(db, batches)

    db.commit()
    return created, merged, skipped


def _create_insight(db: Session, action: dict) -> bool:
    """Create a new insight, using dedup logic to prevent duplicates.

    Dedup logic mirrors routers/insights.py POST endpoint — if either changes,
    update the other. Consider extracting to shared helper if this diverges.

    Returns True if a new insight was created, False if dedup matched an existing one.
    """
    title = action.get("title", "").strip()
    if not title:
        return False

    project = action.get("project")
    insight_type = action.get("insight_type", "learning")
    description = action.get("description")
    severity = action.get("severity", "info")
    confidence = action.get("confidence")
    tags = action.get("tags", [])

    # Dedup: check same title + project within 7 days
    cutoff = datetime.now(UTC) - timedelta(days=INSIGHT_DEDUP_WINDOW_DAYS)
    query = db.query(Insight).filter(
        Insight.title == title,
        Insight.created_at >= cutoff,
    )
    if project is not None:
        query = query.filter(Insight.project == project)
    else:
        query = query.filter(Insight.project.is_(None))

    existing = query.first()

    if existing:
        # Merge into existing
        _append_observation(existing, description or title)
        if confidence is not None:
            existing.confidence = confidence
        db.flush()
        return False

    # Cross-project dedup: check for same title in ANY project
    cross_match = (
        db.query(Insight)
        .filter(
            Insight.title == title,
            Insight.created_at >= cutoff,
        )
        .first()
    )

    if cross_match:
        # Merge into the cross-project match, add project tag
        _append_observation(cross_match, f"[{project}] {description or title}")
        existing_tags = cross_match.tags or []
        if project and project not in existing_tags:
            cross_match.tags = existing_tags + [project]
            flag_modified(cross_match, "tags")
        if confidence is not None:
            cross_match.confidence = confidence
        db.flush()
        return False

    # Create new insight
    insight = Insight(
        insight_type=insight_type,
        title=title,
        description=description,
        project=project,
        severity=severity,
        confidence=confidence,
        tags=tags if tags else None,
        observations=[],
    )
    db.add(insight)
    db.flush()
    return True


def _merge_insight(db: Session, action: dict) -> bool:
    """Merge an observation into an existing insight by ID.

    Returns True if merge succeeded, False if insight not found.
    """
    insight_id = action.get("insight_id")
    observation = action.get("observation", "")

    if not insight_id or not observation:
        return False

    existing = db.query(Insight).filter(Insight.id == insight_id).first()
    if not existing:
        logger.warning("Reflection merge target not found: %s", insight_id)
        return False

    _append_observation(existing, observation)
    db.flush()
    return True


def _append_observation(insight: Insight, text: str) -> None:
    """Append an observation to an insight's observations list."""
    observations = insight.observations or []
    entry = f"{datetime.now(UTC).isoformat()}: {text}"
    observations.append(entry)
    insight.observations = observations
    flag_modified(insight, "observations")


def _stamp_sessions(db: Session, batches: list[ProjectBatch]) -> None:
    """Stamp reflected_at on all processed sessions."""
    now = datetime.now(UTC)
    session_ids = []
    for batch in batches:
        for s in batch.sessions:
            session_ids.append(s.id)

    if not session_ids:
        return

    # Batch update
    db.query(AgentSession).filter(AgentSession.id.in_(session_ids)).update(
        {"reflected_at": now},
        synchronize_session="fetch",
    )


__all__ = ["execute_actions"]
