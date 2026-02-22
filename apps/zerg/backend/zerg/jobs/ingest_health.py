"""Ingest health check — detects stale session ingest.

Runs every 30 minutes. Creates a failure insight when no sessions have been
ingested recently, and a recovery insight when ingest resumes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy.orm import Session as DBSession

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.models.agents import AgentSession

logger = logging.getLogger(__name__)

_THRESHOLD_HOURS = float(os.getenv("INGEST_STALE_THRESHOLD_HOURS", "4"))
_DEDUP_HOURS = 1.0  # Max one insight per hour


def compute_ingest_health(db: DBSession) -> dict:
    """Compute ingest health status. Returns dict matching IngestHealthResponse."""
    threshold_hours = _THRESHOLD_HOURS

    session_count = db.query(func.count(AgentSession.id)).scalar() or 0

    if session_count == 0:
        return {
            "status": "unknown",
            "last_session_at": None,
            "gap_hours": None,
            "threshold_hours": threshold_hours,
            "session_count": 0,
        }

    if threshold_hours == 0:
        return {
            "status": "ok",
            "last_session_at": None,
            "gap_hours": None,
            "threshold_hours": threshold_hours,
            "session_count": session_count,
        }

    # Use COALESCE(ended_at, started_at) to include in-progress sessions
    row = db.execute(text("SELECT MAX(COALESCE(ended_at, started_at)) FROM sessions")).fetchone()

    last_at_raw = row[0] if row else None
    if not last_at_raw:
        return {
            "status": "unknown",
            "last_session_at": None,
            "gap_hours": None,
            "threshold_hours": threshold_hours,
            "session_count": session_count,
        }

    # Parse timestamp
    if isinstance(last_at_raw, datetime):
        last_at = last_at_raw
    else:
        last_at = datetime.fromisoformat(str(last_at_raw))

    # Ensure UTC
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    gap_hours = (now - last_at).total_seconds() / 3600

    is_stale = gap_hours >= threshold_hours

    return {
        "status": "stale" if is_stale else "ok",
        "last_session_at": last_at,
        "gap_hours": round(gap_hours, 2),
        "threshold_hours": threshold_hours,
        "session_count": session_count,
    }


async def run() -> dict[str, Any]:
    """Periodic stale ingest check. Creates insights on state transitions."""
    if _THRESHOLD_HOURS == 0:
        return {"skipped": True, "reason": "INGEST_STALE_THRESHOLD_HOURS=0"}

    with db_session() as db:
        health = compute_ingest_health(db)
        status = health["status"]

        if status not in ("ok", "stale"):
            return {"status": status, "action": "none"}

        if status == "stale":
            # Check dedup: don't create insight if one exists within _DEDUP_HOURS
            from zerg.models.work import Insight

            recent_cutoff = datetime.now(timezone.utc).timestamp() - (_DEDUP_HOURS * 3600)
            recent = (
                db.query(Insight)
                .filter(
                    Insight.title == "Stale ingest detected",
                    Insight.created_at >= datetime.fromtimestamp(recent_cutoff, tz=timezone.utc),
                )
                .first()
            )
            if recent:
                return {"status": "stale", "action": "dedup_skip"}

            db.add(
                Insight(
                    insight_type="failure",
                    title="Stale ingest detected",
                    description=(
                        f"No sessions ingested for {health['gap_hours']:.1f} hours "
                        f"(threshold: {health['threshold_hours']}h). "
                        "Check that the Rust engine (longhouse-engine) is running."
                    ),
                    severity="warning",
                )
            )
            db.commit()
            return {"status": "stale", "action": "insight_created"}

        # status == "ok": check if we were recently stale (recovery)
        from zerg.models.work import Insight

        recent_stale = db.query(Insight).filter(Insight.title == "Stale ingest detected").order_by(Insight.created_at.desc()).first()
        recovery_exists = db.query(Insight).filter(Insight.title == "Ingest recovered").order_by(Insight.created_at.desc()).first()

        if recent_stale and (not recovery_exists or recovery_exists.created_at < recent_stale.created_at):
            db.add(
                Insight(
                    insight_type="learning",
                    title="Ingest recovered",
                    description=f"Session ingest resumed. Last session: {health.get('last_session_at')}",
                    severity="info",
                )
            )
            db.commit()
            return {"status": "ok", "action": "recovery_insight_created"}

        return {"status": "ok", "action": "none"}


if _THRESHOLD_HOURS != 0:
    job_registry.register(
        JobConfig(
            id="ingest-health-check",
            cron=os.getenv("INGEST_HEALTH_CRON", "*/30 * * * *"),
            func=run,
            enabled=True,
            timeout_seconds=30,
            tags=["health", "ingest", "builtin"],
            description="Check session ingest freshness and alert if stale",
        )
    )
