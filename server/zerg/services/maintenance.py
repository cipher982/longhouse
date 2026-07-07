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
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

logger = logging.getLogger(__name__)

# Interval between runner-health reconcile passes (seconds).
RUNNER_HEALTH_RECONCILE_INTERVAL = 120
LIVE_ARCHIVE_OUTBOX_DRAIN_INTERVAL = int(os.getenv("LONGHOUSE_LIVE_ARCHIVE_DRAIN_INTERVAL_SECONDS", "10"))
LIVE_ARCHIVE_OUTBOX_DRAIN_BATCH_SIZE = int(os.getenv("LONGHOUSE_LIVE_ARCHIVE_DRAIN_BATCH_SIZE", "100"))
LIVE_ARCHIVE_OUTBOX_DRAIN_MAX_BATCHES_PER_TICK = int(os.getenv("LONGHOUSE_LIVE_ARCHIVE_DRAIN_MAX_BATCHES_PER_TICK", "3"))
LIVE_ARCHIVE_OUTBOX_DRAIN_TIMEOUT_SECONDS = float(os.getenv("LONGHOUSE_LIVE_ARCHIVE_DRAIN_TIMEOUT_SECONDS", "8"))
LIVE_ARCHIVE_OUTBOX_DRAIN_QUEUE_TIMEOUT_SECONDS = float(os.getenv("LONGHOUSE_LIVE_ARCHIVE_DRAIN_QUEUE_TIMEOUT_SECONDS", "2"))
LIVE_ARCHIVE_OUTBOX_CLEANUP_BATCH_SIZE = int(os.getenv("LONGHOUSE_LIVE_ARCHIVE_OUTBOX_CLEANUP_BATCH_SIZE", "1000"))
LIVE_ARCHIVE_OUTBOX_RETENTION_DAYS = int(os.getenv("LONGHOUSE_LIVE_ARCHIVE_OUTBOX_RETENTION_DAYS", "7"))

_maintenance_task: asyncio.Task | None = None
_live_archive_drain_task: asyncio.Task | None = None


async def _reconcile_runner_health_once() -> None:
    """Run one runner-health reconcile pass in its own DB session."""
    from zerg.database import get_session_factory
    from zerg.services.runner_health_reconciler import reconcile_runner_health

    db = get_session_factory()()
    try:
        await reconcile_runner_health(db)
    finally:
        db.close()


async def _drain_live_archive_outbox_once() -> dict[str, int]:
    """Drain one live archive outbox batch through the archive writer lane."""

    from zerg.database import get_live_session_factory
    from zerg.database import live_store_configured
    from zerg.models.live_store import LiveArchiveOutbox
    from zerg.services.live_archive_outbox import cleanup_drained_live_archive_outbox
    from zerg.services.live_archive_outbox import drain_live_archive_outbox
    from zerg.services.write_serializer import get_write_serializer

    if not live_store_configured():
        return {"processed": 0, "drained": 0, "failed": 0, "cleaned": 0}
    live_session_factory = get_live_session_factory()
    if live_session_factory is None:
        return {"processed": 0, "drained": 0, "failed": 0, "cleaned": 0}

    def _cleanup_drained() -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=LIVE_ARCHIVE_OUTBOX_RETENTION_DAYS)
        with live_session_factory() as live_db:
            cleaned = cleanup_drained_live_archive_outbox(
                live_db,
                older_than=cutoff,
                limit=LIVE_ARCHIVE_OUTBOX_CLEANUP_BATCH_SIZE,
            )
            live_db.commit()
            return cleaned

    with live_session_factory() as live_db:
        pending = (
            live_db.query(LiveArchiveOutbox.id)
            .filter(LiveArchiveOutbox.drained_at.is_(None))
            .order_by(LiveArchiveOutbox.created_at.asc(), LiveArchiveOutbox.id.asc())
            .first()
        )
    if pending is None:
        cleaned = _cleanup_drained()
        return {"processed": 0, "drained": 0, "failed": 0, "cleaned": cleaned}

    def _drain(archive_db):
        totals = {"processed": 0, "drained": 0, "failed": 0}
        max_batches = max(1, LIVE_ARCHIVE_OUTBOX_DRAIN_MAX_BATCHES_PER_TICK)
        for _batch_index in range(max_batches):
            with live_session_factory() as live_db:
                result = drain_live_archive_outbox(
                    live_db,
                    archive_db,
                    limit=LIVE_ARCHIVE_OUTBOX_DRAIN_BATCH_SIZE,
                )
            batch = result.as_dict()
            for key in totals:
                totals[key] += batch[key]
            if batch["processed"] < LIVE_ARCHIVE_OUTBOX_DRAIN_BATCH_SIZE:
                break
        return totals

    try:
        result = await get_write_serializer().execute(
            _drain,
            auto_commit=False,
            label="live-archive-drain",
            timeout_seconds=max(0.1, LIVE_ARCHIVE_OUTBOX_DRAIN_TIMEOUT_SECONDS),
            queue_timeout_seconds=max(0.1, LIVE_ARCHIVE_OUTBOX_DRAIN_QUEUE_TIMEOUT_SECONDS),
        )
    except TimeoutError:
        logger.warning("Live archive outbox drain deferred because archive writer is saturated", exc_info=True)
        return {"processed": 0, "drained": 0, "failed": 0, "cleaned": 0, "deferred": 1}
    result["cleaned"] = _cleanup_drained()
    return result


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(RUNNER_HEALTH_RECONCILE_INTERVAL)
            await _reconcile_runner_health_once()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("Maintenance tick failed (non-fatal)", exc_info=True)


async def _live_archive_drain_loop() -> None:
    while True:
        try:
            await asyncio.sleep(LIVE_ARCHIVE_OUTBOX_DRAIN_INTERVAL)
            result = await _drain_live_archive_outbox_once()
            if result["processed"] or result["cleaned"]:
                logger.info(
                    "Live archive outbox drain processed=%d drained=%d failed=%d cleaned=%d",
                    result["processed"],
                    result["drained"],
                    result["failed"],
                    result["cleaned"],
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("Live archive outbox drain tick failed (non-fatal)", exc_info=True)


def start_maintenance_loop() -> None:
    """Start the periodic maintenance loop. Idempotent."""
    global _live_archive_drain_task, _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        pass
    else:
        _maintenance_task = asyncio.create_task(_loop())
    if _live_archive_drain_task and not _live_archive_drain_task.done():
        pass
    else:
        _live_archive_drain_task = asyncio.create_task(_live_archive_drain_loop())
    logger.info("Maintenance loop started (runner-health reconcile every %ds)", RUNNER_HEALTH_RECONCILE_INTERVAL)


async def stop_maintenance_loop() -> None:
    """Stop the periodic maintenance loop."""
    global _live_archive_drain_task, _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        _maintenance_task.cancel()
        try:
            await _maintenance_task
        except Exception:
            pass
    if _live_archive_drain_task and not _live_archive_drain_task.done():
        _live_archive_drain_task.cancel()
        try:
            await _live_archive_drain_task
        except Exception:
            pass
    _maintenance_task = None
    _live_archive_drain_task = None
