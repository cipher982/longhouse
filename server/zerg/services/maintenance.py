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
    from zerg.database import get_session_factory
    from zerg.services.runner_health_reconciler import reconcile_runner_health

    db = get_session_factory()()
    try:
        await reconcile_runner_health(db)
    finally:
        db.close()


async def _process_queued_notifications_once() -> None:
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
