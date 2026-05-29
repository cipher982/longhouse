"""Periodic runtime maintenance loop.

Replaces the old generic jobs scheduler. A single asyncio loop runs the small
set of recurring tasks the Runtime Host needs to keep itself healthy. No cron,
no durable queue, no external manifest — just interval-driven upkeep that starts
unconditionally with the server.

WAL checkpointing lives in ``database.start_wal_checkpoint_loop`` and is not
duplicated here.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Interval between runner-health reconcile passes (seconds).
RUNNER_HEALTH_RECONCILE_INTERVAL = 120

_maintenance_task: asyncio.Task | None = None


async def _reconcile_runner_health_once() -> None:
    """Run one runner-health reconcile pass in its own DB session."""
    from zerg.database import get_session_factory
    from zerg.services.runner_health_reconciler import reconcile_runner_health

    db = get_session_factory()()
    try:
        await reconcile_runner_health(db)
    finally:
        db.close()


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(RUNNER_HEALTH_RECONCILE_INTERVAL)
            await _reconcile_runner_health_once()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("Maintenance tick failed (non-fatal)", exc_info=True)


def start_maintenance_loop() -> None:
    """Start the periodic maintenance loop. Idempotent."""
    global _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        return
    _maintenance_task = asyncio.create_task(_loop())
    logger.info("Maintenance loop started (runner-health reconcile every %ds)", RUNNER_HEALTH_RECONCILE_INTERVAL)


async def stop_maintenance_loop() -> None:
    """Stop the periodic maintenance loop."""
    global _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        _maintenance_task.cancel()
        try:
            await _maintenance_task
        except Exception:
            pass
    _maintenance_task = None
