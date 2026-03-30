"""Ingest health check — detects stale session ingest.

Runs every 30 minutes. Opens an operational incident when session ingest goes
stale and resolves it when ingest resumes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy.orm import Session as DBSession

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.models.agents import AgentSession
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_OPEN
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_RESOLVED
from zerg.models.work import OperationalIncident

logger = logging.getLogger(__name__)

_THRESHOLD_HOURS = float(os.getenv("INGEST_STALE_THRESHOLD_HOURS", "4"))
_ONLINE_THRESHOLD_MINUTES = int(os.getenv("DEVICE_ONLINE_THRESHOLD_MINUTES", "15"))
INCIDENT_SOURCE = "ingest_health"
INCIDENT_TYPE = "stale_ingest"
INCIDENT_DEDUPE_KEY = "ingest-health:stale"


def _is_any_device_online(db: DBSession, now: datetime) -> tuple[bool, datetime | None]:
    """Return (is_online, last_heartbeat_at).

    Online means a non-offline heartbeat was received within _ONLINE_THRESHOLD_MINUTES.
    If no heartbeats have ever been sent (new install), returns (False, None).
    """
    cutoff = now - timedelta(minutes=_ONLINE_THRESHOLD_MINUTES)
    row = db.execute(
        text("SELECT MAX(received_at) FROM agent_heartbeats " "WHERE received_at > :cutoff AND is_offline = 0"),
        {"cutoff": cutoff},
    ).fetchone()
    if not row or row[0] is None:
        return False, None
    last_hb = row[0]
    if isinstance(last_hb, str):
        last_hb = datetime.fromisoformat(last_hb)
    if last_hb.tzinfo is None:
        last_hb = last_hb.replace(tzinfo=timezone.utc)
    return True, last_hb


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

    if not is_stale:
        return {
            "status": "ok",
            "last_session_at": last_at,
            "gap_hours": round(gap_hours, 2),
            "threshold_hours": threshold_hours,
            "session_count": session_count,
            "device_online": None,
            "last_heartbeat_at": None,
        }

    # Gap is over threshold — only a real problem if the device is actually online.
    # If there's no recent heartbeat, the laptop/device is off or sleeping.
    now = datetime.now(timezone.utc)
    device_online, last_heartbeat_at = _is_any_device_online(db, now)
    if not device_online:
        return {
            "status": "device_offline",
            "last_session_at": last_at,
            "gap_hours": round(gap_hours, 2),
            "threshold_hours": threshold_hours,
            "session_count": session_count,
            "device_online": False,
            "last_heartbeat_at": None,
        }

    return {
        "status": "stale",
        "last_session_at": last_at,
        "gap_hours": round(gap_hours, 2),
        "threshold_hours": threshold_hours,
        "session_count": session_count,
        "device_online": True,
        "last_heartbeat_at": last_heartbeat_at,
    }


async def run() -> dict[str, Any]:
    """Periodic stale ingest check. Opens or resolves incidents on state transitions."""
    if _THRESHOLD_HOURS == 0:
        return {"skipped": True, "reason": "INGEST_STALE_THRESHOLD_HOURS=0"}

    with db_session() as db:
        health = compute_ingest_health(db)
        status = health["status"]
        now = datetime.now(timezone.utc)
        open_incident = (
            db.query(OperationalIncident)
            .filter(
                OperationalIncident.dedupe_key == INCIDENT_DEDUPE_KEY,
                OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
            )
            .order_by(OperationalIncident.opened_at.desc())
            .first()
        )

        if status not in ("ok", "stale", "device_offline"):
            return {"status": status, "action": "none"}

        if status == "device_offline":
            # Device is off/sleeping — ingest gap is expected, not actionable.
            # If a stale incident was open before the device went offline, close it.
            if open_incident is not None:
                open_incident.status = OPERATIONAL_INCIDENT_STATUS_RESOLVED
                open_incident.summary = "Session ingest stale but device is offline (expected)"
                open_incident.last_observed_at = now
                open_incident.resolved_at = now
                open_incident.context = {
                    **dict(open_incident.context or {}),
                    "resolved_at": now.isoformat(),
                    "resolved_reason": "device_offline",
                }
                db.commit()
                return {"status": "device_offline", "action": "incident_resolved"}
            return {"status": "device_offline", "action": "none"}

        if status == "stale":
            summary = f"No sessions ingested for {health['gap_hours']:.1f} hours (threshold: {health['threshold_hours']}h)."
            context = {
                "gap_hours": health["gap_hours"],
                "threshold_hours": health["threshold_hours"],
                "last_session_at": health["last_session_at"].isoformat() if health["last_session_at"] else None,
                "session_count": health["session_count"],
                "observed_at": now.isoformat(),
                "recommended_action": "Check that the Rust engine (longhouse-engine) is running.",
            }
            if open_incident is None:
                db.add(
                    OperationalIncident(
                        incident_type=INCIDENT_TYPE,
                        source=INCIDENT_SOURCE,
                        dedupe_key=INCIDENT_DEDUPE_KEY,
                        status=OPERATIONAL_INCIDENT_STATUS_OPEN,
                        summary=summary,
                        context=context,
                        opened_at=now,
                        last_observed_at=now,
                    )
                )
                db.commit()
                return {"status": "stale", "action": "incident_opened"}

            open_incident.summary = summary
            open_incident.context = context
            open_incident.last_observed_at = now
            db.commit()
            return {"status": "stale", "action": "incident_updated"}

        if open_incident is not None:
            open_incident.status = OPERATIONAL_INCIDENT_STATUS_RESOLVED
            open_incident.summary = "Session ingest recovered"
            open_incident.last_observed_at = now
            open_incident.resolved_at = now
            open_incident.context = {
                **dict(open_incident.context or {}),
                "resolved_at": now.isoformat(),
                "resolved_last_session_at": health["last_session_at"].isoformat() if health["last_session_at"] else None,
            }
            db.commit()
            return {"status": "ok", "action": "incident_resolved"}

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
