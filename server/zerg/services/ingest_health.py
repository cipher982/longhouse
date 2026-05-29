"""Ingest health computation — detects stale session ingest.

Pure read-only health check surfaced by the agents-backfill router. The old
incident-writing job wrapper was removed with the jobs scheduler teardown.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy.orm import Session as DBSession

from zerg.models.agents import AgentSession

_THRESHOLD_HOURS = float(os.getenv("INGEST_STALE_THRESHOLD_HOURS", "4"))
_ONLINE_THRESHOLD_MINUTES = int(os.getenv("DEVICE_ONLINE_THRESHOLD_MINUTES", "15"))


def _is_any_device_online(db: DBSession, now: datetime) -> tuple[bool, datetime | None]:
    """Return (is_online, last_heartbeat_at).

    Online means a non-offline heartbeat was received within _ONLINE_THRESHOLD_MINUTES.
    If no heartbeats have ever been sent (new install), returns (False, None).
    """
    cutoff = now - timedelta(minutes=_ONLINE_THRESHOLD_MINUTES)
    row = db.execute(
        text("SELECT MAX(received_at) FROM agent_heartbeats WHERE received_at > :cutoff AND is_offline = 0"),
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
