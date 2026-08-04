"""Small periodic Runtime Host maintenance loop.

Storage-v2 projectors own derived transcript work. This loop intentionally has
no archive writer, archive outbox, or cold-database responsibility.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

RUNNER_HEALTH_RECONCILE_INTERVAL = 120
_maintenance_task: asyncio.Task | None = None


async def _reconcile_runner_health_once() -> None:
    from zerg.database import live_catalog_enabled

    if live_catalog_enabled():
        from zerg.crud import runner_crud
        from zerg.services import runner_catalog
        from zerg.services.runner_connection_manager import get_runner_connection_manager
        from zerg.services.runner_health import assess_runner_health
        from zerg.services.runner_health import runner_requires_proactive_attention
        from zerg.services.runner_health_reconciler import ALERT_AFTER
        from zerg.services.runner_health_reconciler import ALERT_CLAIM_TTL
        from zerg.services.runner_health_reconciler import ALERT_REASON_CHANGE_COOLDOWN
        from zerg.services.runner_health_reconciler import _build_external_alert_copy
        from zerg.services.runner_health_reconciler import _open_incident_context
        from zerg.services.runner_health_reconciler import _send_email_alert
        from zerg.utils.time import utc_now_naive

        connection_manager = get_runner_connection_manager()
        now = utc_now_naive()
        for runner in runner_crud.get_runners(None, owner_id=0, limit=10_000):
            health = assess_runner_health(
                runner,
                now=now,
                is_connected=connection_manager.is_online(runner.owner_id, runner.id),
            )
            applied = runner_catalog.operation(
                "health_apply",
                runner_id=runner.id,
                effective_status=health.effective_status,
                reason_code=health.status_reason,
                summary=health.status_summary,
                proactive_attention=runner_requires_proactive_attention(health.availability_policy),
                context=_open_incident_context(runner, health, now),
                observed_at=now.isoformat(),
            )
            incident = runner_catalog.incident(applied.get("incident"))
            owner = runner_catalog.user(applied.get("owner"))
            incident_context = dict(incident.context or {}) if incident is not None else {}
            alert_reason_changed = (
                incident is not None
                and incident.alert_sent_at is not None
                and incident_context.get("alert_reason_code") not in {None, health.status_reason}
            )
            alert_reason_change_cooled_down = (
                not alert_reason_changed
                or incident is None
                or incident.alert_sent_at is None
                or now - incident.alert_sent_at >= ALERT_REASON_CHANGE_COOLDOWN
            )
            if (
                incident is not None
                and incident.status == "open"
                and (incident.alert_sent_at is None or (alert_reason_changed and alert_reason_change_cooled_down))
                and owner is not None
                and now - incident.opened_at >= ALERT_AFTER
            ):
                subject, body = _build_external_alert_copy(runner, health, incident, now)
                claim = runner_catalog.operation(
                    "health_alert_claim",
                    incident_id=incident.id,
                    reason_code=health.status_reason,
                    observed_at=now.isoformat(),
                    claim_ttl_seconds=int(ALERT_CLAIM_TTL.total_seconds()),
                )
                if claim.get("claimed") is True and _send_email_alert(owner, subject, body):
                    runner_catalog.operation(
                        "health_alert_sent",
                        incident_id=incident.id,
                        reason_code=health.status_reason,
                        observed_at=now.isoformat(),
                    )
        return

    from zerg.database import get_catalog_session_factory
    from zerg.services.runner_health_reconciler import reconcile_runner_health

    db = get_catalog_session_factory()()
    try:
        await reconcile_runner_health(db)
    finally:
        db.close()


async def _process_queued_notifications_once() -> None:
    from zerg.database import live_catalog_enabled

    if live_catalog_enabled():
        return

    from zerg.database import get_session_factory
    from zerg.services.notification_queue import process_queued_notification_events

    db = get_session_factory()()
    try:
        await process_queued_notification_events(db)
    finally:
        db.close()


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(RUNNER_HEALTH_RECONCILE_INTERVAL)
            await _reconcile_runner_health_once()
            await _process_queued_notifications_once()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("Maintenance tick failed (non-fatal)", exc_info=True)


def start_maintenance_loop() -> None:
    """Start the periodic non-storage maintenance loop. Idempotent."""

    global _maintenance_task
    if _maintenance_task is None or _maintenance_task.done():
        _maintenance_task = asyncio.create_task(_loop())
    logger.info("Maintenance loop started (runner-health reconcile every %ds)", RUNNER_HEALTH_RECONCILE_INTERVAL)


async def stop_maintenance_loop() -> None:
    """Stop the periodic maintenance loop."""

    global _maintenance_task
    if _maintenance_task is not None and not _maintenance_task.done():
        _maintenance_task.cancel()
        try:
            await _maintenance_task
        except asyncio.CancelledError:
            pass
    _maintenance_task = None
