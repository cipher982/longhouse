"""Stale agent detection job.

Runs hourly. Queries agent_heartbeats for devices that haven't checked in
within the last 30 minutes and opens or resolves operational incidents.
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
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_OPEN
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_RESOLVED
from zerg.models.work import OperationalIncident

logger = logging.getLogger(__name__)

STALE_THRESHOLD_MINUTES = 30
INCIDENT_SOURCE = "check_stale_agents"
INCIDENT_TYPE = "stale_agent"


def _incident_summary(*, device_id: str, elapsed_minutes: int) -> str:
    return (
        f"Agent {device_id} has not checked in for {elapsed_minutes} minutes "
        f"(threshold: {STALE_THRESHOLD_MINUTES} minutes)."
    )


def _incident_context(*, device_id: str, elapsed_minutes: int, last_seen: datetime, now: datetime) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "elapsed_minutes": elapsed_minutes,
        "threshold_minutes": STALE_THRESHOLD_MINUTES,
        "last_seen_at": last_seen.isoformat(),
        "observed_at": now.isoformat(),
    }


async def run() -> dict[str, Any]:
    """Check for agents that have missed heartbeats."""
    import sqlalchemy

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_THRESHOLD_MINUTES)

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
                elapsed = now - last_seen
                elapsed_minutes = int(elapsed.total_seconds() / 60)
            except Exception:
                continue

            stale_devices.append(
                {
                    "device_id": device_id,
                    "elapsed_minutes": elapsed_minutes,
                    "last_seen": last_seen,
                }
            )

        incidents_opened = 0
        incidents_updated = 0
        incidents_resolved = 0
        stale_dedupe_keys = {f"stale-agent:{device['device_id']}" for device in stale_devices}

        for device in stale_devices:
            dedupe_key = f"stale-agent:{device['device_id']}"
            existing = (
                db.query(OperationalIncident)
                .filter(
                    OperationalIncident.dedupe_key == dedupe_key,
                    OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
                )
                .order_by(OperationalIncident.opened_at.desc())
                .first()
            )
            summary = _incident_summary(device_id=device["device_id"], elapsed_minutes=device["elapsed_minutes"])
            context = _incident_context(
                device_id=device["device_id"],
                elapsed_minutes=device["elapsed_minutes"],
                last_seen=device["last_seen"],
                now=now,
            )
            if existing is None:
                db.add(
                    OperationalIncident(
                        incident_type=INCIDENT_TYPE,
                        source=INCIDENT_SOURCE,
                        dedupe_key=dedupe_key,
                        status=OPERATIONAL_INCIDENT_STATUS_OPEN,
                        summary=summary,
                        context=context,
                        opened_at=now,
                        last_observed_at=now,
                    )
                )
                incidents_opened += 1
                continue

            existing.summary = summary
            existing.context = context
            existing.last_observed_at = now
            incidents_updated += 1

        open_incidents = (
            db.query(OperationalIncident)
            .filter(
                OperationalIncident.source == INCIDENT_SOURCE,
                OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
            )
            .all()
        )
        for incident in open_incidents:
            if incident.dedupe_key in stale_dedupe_keys:
                continue
            context = dict(incident.context or {})
            incident.status = OPERATIONAL_INCIDENT_STATUS_RESOLVED
            incident.summary = f"Agent {context.get('device_id', 'unknown')} heartbeat recovered"
            incident.last_observed_at = now
            incident.resolved_at = now
            incident.context = {
                **context,
                "resolved_at": now.isoformat(),
                "resolved": True,
            }
            incidents_resolved += 1

        db.commit()

    logger.info(f"check_stale_agents: {len(stale_devices)} stale device(s)")
    return {
        "success": True,
        "stale_devices": len(stale_devices),
        "incidents_opened": incidents_opened,
        "incidents_updated": incidents_updated,
        "incidents_resolved": incidents_resolved,
    }


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
