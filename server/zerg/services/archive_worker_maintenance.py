"""Bounded cold maintenance scheduled inside the archive worker process."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _interval(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


class ArchiveMaintenanceScheduler:
    """Run at most one due maintenance unit between foreground worker units."""

    def __init__(self) -> None:
        now = time.monotonic()
        self._next_due = {
            "catalog_sync": now + 2.0,
            "projection": now + _interval("SESSION_PROJECTION_POLL_SECONDS", 5.0),
            "summary": now + _interval("SESSION_SUMMARY_POLL_SECONDS", 15.0),
            "recall": now + _interval("LONGHOUSE_RECALL_WORKER_POLL_SECONDS", 2.0),
            "preview_cleanup": now + 60.0,
            "archive_wal": now + _interval("LONGHOUSE_WAL_CHECKPOINT_INTERVAL_SECONDS", 30.0),
        }
        self._order = tuple(self._next_due)
        self._cursor = 0

    async def run_one_due(
        self,
        *,
        on_start: Callable[[str], None] | None = None,
        allow_expensive: bool = True,
    ) -> str | None:
        now = time.monotonic()
        for offset in range(len(self._order)):
            index = (self._cursor + offset) % len(self._order)
            name = self._order[index]
            if now < self._next_due[name]:
                continue
            if not allow_expensive and name in {"projection", "summary"}:
                continue
            self._cursor = (index + 1) % len(self._order)
            interval, operation = self._operation(name)
            self._next_due[name] = now + interval
            if operation is None:
                continue
            if on_start is not None:
                on_start(name)
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Archive worker maintenance failed: %s", name)
            return name
        return None

    def _operation(self, name: str) -> tuple[float, Callable[[], Awaitable[None]] | None]:
        if name == "catalog_sync":

            async def run_catalog_sync() -> None:
                from zerg.services.live_catalog_backfill import sync_recent_live_catalog

                await asyncio.to_thread(sync_recent_live_catalog, limit=25)

            return 2.0, run_catalog_sync
        if name == "projection":
            interval = _interval("SESSION_PROJECTION_POLL_SECONDS", 5.0)

            async def run_projection() -> None:
                from zerg.services.session_projection_reconciler import reconcile_projection_lag_once

                await reconcile_projection_lag_once(limit=1)

            return interval, run_projection
        if name == "summary":
            interval = _interval("SESSION_SUMMARY_POLL_SECONDS", 15.0)

            async def run_summary() -> None:
                from zerg.services.session_enrichment_reconciler import reconcile_summaries_once

                timeout = _interval("LONGHOUSE_ARCHIVE_SUMMARY_TICK_TIMEOUT_SECONDS", 30.0)
                await asyncio.wait_for(
                    reconcile_summaries_once(limit=1, concurrency=1),
                    timeout=max(1.0, timeout),
                )

            return interval, run_summary
        if name == "recall":
            interval = _interval("LONGHOUSE_RECALL_WORKER_POLL_SECONDS", 2.0)

            async def run_recall() -> None:
                from zerg.services.retrieval_index_jobs import run_recall_index_job_once

                await asyncio.to_thread(run_recall_index_job_once)

            return interval, run_recall
        if name == "preview_cleanup":
            interval = 60.0
            enabled = os.getenv("LONGHOUSE_ENABLE_LIVE_PREVIEW_CLEANUP", "").strip().lower()
            if enabled not in {"1", "true", "yes", "on"}:
                return interval, None

            async def run_preview_cleanup() -> None:
                from zerg.services.provisional_events import cleanup_bridge_transcript_preview_observations
                from zerg.services.write_serializer import get_write_serializer

                await get_write_serializer().execute(
                    lambda db: cleanup_bridge_transcript_preview_observations(
                        db,
                        batch_size=100,
                        max_sessions=2,
                    ),
                    label="live-preview-cleanup",
                    timeout_seconds=5.0,
                )

            return interval, run_preview_cleanup
        if name == "archive_wal":
            interval = _interval("LONGHOUSE_WAL_CHECKPOINT_INTERVAL_SECONDS", 30.0)

            async def run_archive_wal() -> None:
                from zerg.database import run_archive_wal_checkpoint_once

                await asyncio.to_thread(run_archive_wal_checkpoint_once)

            return interval, run_archive_wal
        raise ValueError(f"unknown archive maintenance operation: {name}")
