"""Stale agent detection job.

Runs hourly. Queries agent_heartbeats for devices that haven't checked in
within the last 30 minutes and emits a Longhouse insight per stale device.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.models.work import Insight

logger = logging.getLogger(__name__)

STALE_THRESHOLD_MINUTES = 30


async def run() -> dict[str, Any]:
    """Check for agents that have missed heartbeats."""
    import sqlalchemy

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_THRESHOLD_MINUTES)

    stale_devices: list[dict[str, Any]] = []

    with db_session() as db:
        # Get the latest heartbeat per device (SQLite-compatible)
        try:
            rows = db.execute(
                sqlalchemy.text(
                    """
                    SELECT device_id, MAX(received_at) as last_seen
                    FROM agent_heartbeats
                    GROUP BY device_id
                    HAVING MAX(received_at) < :cutoff
                    """
                ),
                # Use space-separated format to match SQLAlchemy's SQLite datetime storage
                {"cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")},
            ).fetchall()
        except Exception as e:
            logger.warning(f"check_stale_agents query failed (table may not exist yet): {e}")
            return {"success": True, "stale_devices": 0}

        for row in rows:
            device_id, last_seen_val = row[0], row[1]
            try:
                if isinstance(last_seen_val, str):
                    last_seen = datetime.fromisoformat(last_seen_val.replace("Z", "+00:00"))
                elif isinstance(last_seen_val, datetime):
                    last_seen = last_seen_val
                else:
                    continue
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                elapsed = datetime.now(timezone.utc) - last_seen
                elapsed_minutes = int(elapsed.total_seconds() / 60)
            except Exception:
                elapsed_minutes = -1

            stale_devices.append({"device_id": device_id, "elapsed_minutes": elapsed_minutes})

        # Emit one insight per stale device (deduped by title in 7-day window)
        for device in stale_devices:
            title = f"Agent {device['device_id']} has not checked in"
            description = (
                f"Engine daemon on device '{device['device_id']}' has not sent a heartbeat "
                f"for {device['elapsed_minutes']} minutes "
                f"(threshold: {STALE_THRESHOLD_MINUTES} minutes)."
            )
            try:
                # Check for recent duplicate
                from zerg.models.work import INSIGHT_DEDUP_WINDOW_DAYS

                dedup_cutoff = datetime.now(timezone.utc) - timedelta(days=INSIGHT_DEDUP_WINDOW_DAYS)
                existing = db.query(Insight).filter(Insight.title == title, Insight.created_at >= dedup_cutoff).first()
                if existing:
                    obs = existing.observations or []
                    obs.append(f"{datetime.now(timezone.utc).isoformat()}: {description}")
                    existing.observations = obs
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(existing, "observations")
                else:
                    insight = Insight(
                        insight_type="failure",
                        title=title,
                        description=description,
                        severity="warning",
                        tags=["engine", "heartbeat", "stale-agent"],
                    )
                    db.add(insight)
                db.commit()
            except Exception as e:
                logger.warning(f"Failed to log stale agent insight: {e}")
                db.rollback()

    logger.info(f"check_stale_agents: {len(stale_devices)} stale device(s)")
    return {"success": True, "stale_devices": len(stale_devices)}


# Register the job
job_registry.register(
    JobConfig(
        id="check-stale-agents",
        cron=os.getenv("STALE_AGENTS_CRON", "0 * * * *"),  # hourly
        func=run,
        enabled=True,
        timeout_seconds=60,
        tags=["heartbeat", "monitoring", "builtin"],
        description="Detect engine daemons that have stopped sending heartbeats",
    )
)
