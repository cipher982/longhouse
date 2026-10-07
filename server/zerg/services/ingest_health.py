"""Ingest health computation — detects stale session ingest.

Pure read-only health check surfaced by the agents-backfill router. The old
incident-writing job wrapper was removed with the jobs scheduler teardown.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

_THRESHOLD_HOURS = float(os.getenv("INGEST_STALE_THRESHOLD_HOURS", "4"))
_ONLINE_THRESHOLD_MINUTES = int(os.getenv("DEVICE_ONLINE_THRESHOLD_MINUTES", "15"))


def compute_ingest_health_from_catalog_facts(facts: dict, *, now: datetime | None = None) -> dict:
    """Project bounded catalogd facts into the public ingest-health contract."""

    observed_now = now or datetime.now(timezone.utc)
    session_count = int(facts.get("session_count") or 0)
    threshold_hours = _THRESHOLD_HOURS
    last_session_at = _catalog_datetime(facts.get("last_session_at"))
    last_heartbeat_at = _catalog_datetime(facts.get("last_heartbeat_at"))
    result = {
        "status": "unknown",
        "last_session_at": last_session_at,
        "gap_hours": None,
        "threshold_hours": threshold_hours,
        "session_count": session_count,
        "media_repair_refs": int(facts.get("media_repair_refs") or 0),
        "media_repair_bytes": int(facts.get("media_repair_bytes") or 0),
    }
    if session_count == 0 or last_session_at is None:
        return result
    if threshold_hours == 0:
        result["status"] = "ok"
        return result
    gap_hours = max(0.0, (observed_now - last_session_at).total_seconds() / 3600)
    result["gap_hours"] = round(gap_hours, 2)
    if gap_hours < threshold_hours:
        result["status"] = "ok"
        return result
    heartbeat_cutoff = observed_now - timedelta(minutes=_ONLINE_THRESHOLD_MINUTES)
    result["status"] = "stale" if last_heartbeat_at is not None and last_heartbeat_at >= heartbeat_cutoff else "device_offline"
    return result


def _catalog_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
