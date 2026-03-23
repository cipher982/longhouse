"""Background reconciler for hosted instance health.

Probes all provisioning/active/unhealthy instances on a schedule and:
- Promotes provisioning -> active on first successful /api/readyz
- Times out provisioning -> failed after PROVISIONING_TIMEOUT_MINUTES
- Increments consecutive_failures for active/unhealthy instances that fail
- Marks active -> unhealthy after UNHEALTHY_THRESHOLD consecutive failures
- Recovers unhealthy -> active when the instance passes again

Modeled on server/zerg/services/runner_health_reconciler.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import httpx

from control_plane.db import SessionLocal
from control_plane.models import Instance

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 300  # 5 minutes
PROVISIONING_TIMEOUT_MINUTES = 15
UNHEALTHY_THRESHOLD = 3  # consecutive failures before marking unhealthy
PROBE_TIMEOUT_SECONDS = 5.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _readyz_url(subdomain: str, root_domain: str) -> str:
    return f"https://{subdomain}.{root_domain}/api/readyz"


def _probe(url: str) -> tuple[bool, str | None]:
    try:
        resp = httpx.get(url, timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=True)
        if resp.status_code == 200:
            return True, None
        return False, f"status={resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def reconcile_once(root_domain: str) -> dict:
    """Probe all tracked instances once. Returns a summary dict."""
    db = SessionLocal()
    try:
        return _reconcile(db, root_domain)
    finally:
        db.close()


def _reconcile(db, root_domain: str) -> dict:
    now = _utcnow()
    result = {
        "checked": 0,
        "promoted": 0,
        "provisioning_timed_out": 0,
        "failures_recorded": 0,
        "marked_unhealthy": 0,
        "recovered": 0,
        "errors": 0,
    }

    instances = (
        db.query(Instance)
        .filter(Instance.status.in_(["provisioning", "active", "unhealthy"]))
        .all()
    )

    for inst in instances:
        try:
            url = _readyz_url(inst.subdomain, root_domain)
            healthy, error = _probe(url)
            result["checked"] += 1

            if inst.status == "provisioning":
                if healthy:
                    inst.status = "active"
                    inst.last_health_at = now
                    inst.consecutive_failures = 0
                    inst.last_health_error = None
                    result["promoted"] += 1
                else:
                    # Detect stuck provisioning
                    created = inst.created_at
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age = now - created
                    if age > timedelta(minutes=PROVISIONING_TIMEOUT_MINUTES):
                        inst.status = "failed"
                        inst.last_health_error = (
                            f"Provisioning timeout after {PROVISIONING_TIMEOUT_MINUTES}m: {error}"
                        )
                        result["provisioning_timed_out"] += 1

            else:  # active or unhealthy
                if healthy:
                    inst.last_health_at = now
                    inst.consecutive_failures = 0
                    inst.last_health_error = None
                    if inst.status == "unhealthy":
                        inst.status = "active"
                        inst.unhealthy_since = None
                        result["recovered"] += 1
                else:
                    inst.consecutive_failures = (inst.consecutive_failures or 0) + 1
                    inst.last_health_error = error
                    result["failures_recorded"] += 1
                    if inst.consecutive_failures >= UNHEALTHY_THRESHOLD and inst.status != "unhealthy":
                        inst.status = "unhealthy"
                        inst.unhealthy_since = now
                        result["marked_unhealthy"] += 1
                        logger.warning(
                            "Instance %s marked unhealthy after %d consecutive failures: %s",
                            inst.subdomain,
                            inst.consecutive_failures,
                            error,
                        )

            db.commit()
        except Exception:
            db.rollback()
            result["errors"] += 1
            logger.exception("Instance health reconcile failed for %s", inst.subdomain)

    return result


async def run_instance_health_reconciler(root_domain: str) -> None:
    """Async loop: probe all hosted instances every RECONCILE_INTERVAL_SECONDS."""
    logger.info(
        "Instance health reconciler started (interval=%ds, timeout=%dm, unhealthy_threshold=%d)",
        RECONCILE_INTERVAL_SECONDS,
        PROVISIONING_TIMEOUT_MINUTES,
        UNHEALTHY_THRESHOLD,
    )
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            result = reconcile_once(root_domain)
            actionable = {k: v for k, v in result.items() if k != "checked" and v > 0}
            if actionable or result.get("errors", 0) > 0:
                logger.info("Instance health reconcile: checked=%d %s", result["checked"], actionable)
            else:
                logger.debug("Instance health reconcile: checked=%d all ok", result["checked"])
        except Exception:
            logger.exception("Instance health reconciler loop error")
