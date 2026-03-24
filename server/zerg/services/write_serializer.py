"""Single-writer serializer for SQLite.

SQLite permits only one writer at a time. When multiple background tasks
(ingest, presence flush, task queue, commis jobs) all compete for the write
lock via separate threads, `database is locked` errors and long waits result.

This module serializes ALL database writes through a single asyncio.Lock.
While one write executes, others wait in the queue. Reads are unaffected
(WAL mode allows unlimited concurrent readers).

Usage:
    from zerg.services.write_serializer import write_serializer

    # In async context:
    result = await write_serializer.execute(lambda db: do_writes(db))

    # With priority (lower = higher priority):
    await write_serializer.execute(fn, label="presence-flush")

Architecture:
    - asyncio.Lock ensures one write at a time (FIFO)
    - Each write runs in asyncio.to_thread() to avoid blocking the event loop
    - The write function receives a fresh Session, does its work, and returns
    - Session lifecycle (commit on success, rollback on error, close) is managed
      by the serializer
    - A dedicated write engine with StaticPool (single connection) prevents
      connection pool contention
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable
from typing import TypeVar

from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class WriteStats:
    """Cumulative write serializer statistics."""

    total_writes: int = 0
    total_queue_wait_ms: float = 0
    total_exec_ms: float = 0
    max_queue_wait_ms: float = 0
    max_exec_ms: float = 0
    errors: int = 0
    _label_counts: dict[str, int] = field(default_factory=dict)


class WriteSerializer:
    """Serialize all SQLite writes through a single lock.

    Ensures only one write transaction runs at a time, eliminating
    'database is locked' errors from concurrent background writers.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._session_factory: sessionmaker | None = None
        self._stats = WriteStats()
        self._configured = False

    def configure(self, session_factory: sessionmaker) -> None:
        """Set the write session factory. Call once at startup."""
        self._session_factory = session_factory
        self._configured = True
        logger.info("WriteSerializer configured")

    @property
    def is_configured(self) -> bool:
        return self._configured

    @property
    def stats(self) -> WriteStats:
        return self._stats

    async def execute(
        self,
        fn: Callable[[Session], T],
        *,
        label: str = "",
        auto_commit: bool = True,
    ) -> T:
        """Submit a write operation. Serialized via asyncio.Lock.

        Args:
            fn: Function receiving a Session. Do reads + writes here.
            label: Optional label for logging/metrics.
            auto_commit: If True, commit after fn returns. Set False if fn
                         manages its own commits (e.g. chunked ingest).

        Returns:
            Whatever fn returns.

        Raises:
            Whatever fn raises (after rollback).
        """
        if not self._configured:
            raise RuntimeError("WriteSerializer not configured — call configure() first")

        t0 = time.monotonic()

        async with self._lock:
            t1 = time.monotonic()
            queue_wait_ms = (t1 - t0) * 1000

            try:
                result = await asyncio.to_thread(self._run, fn, auto_commit, label)
            except Exception:
                self._stats.errors += 1
                raise
            finally:
                t2 = time.monotonic()
                exec_ms = (t2 - t1) * 1000

                self._stats.total_writes += 1
                self._stats.total_queue_wait_ms += queue_wait_ms
                self._stats.total_exec_ms += exec_ms
                self._stats.max_queue_wait_ms = max(self._stats.max_queue_wait_ms, queue_wait_ms)
                self._stats.max_exec_ms = max(self._stats.max_exec_ms, exec_ms)
                if label:
                    self._stats._label_counts[label] = self._stats._label_counts.get(label, 0) + 1

                if queue_wait_ms > 500:
                    logger.warning(
                        "WriteSerializer: %s waited %.0fms in queue, exec %.0fms",
                        label or "unlabeled",
                        queue_wait_ms,
                        exec_ms,
                    )

            return result

    def execute_sync(
        self,
        fn: Callable[[Session], T],
        *,
        label: str = "",
        auto_commit: bool = True,
    ) -> T:
        """Synchronous write for non-async contexts (startup, migrations).

        NOT serialized with async writes — only use during startup before
        the event loop is serving requests.
        """
        if not self._configured:
            raise RuntimeError("WriteSerializer not configured — call configure() first")
        return self._run(fn, auto_commit, label)

    def _run(self, fn: Callable[[Session], T], auto_commit: bool, label: str) -> T:
        """Execute fn with a fresh session. Runs in a worker thread."""
        db = self._session_factory()
        try:
            result = fn(db)
            if auto_commit:
                db.commit()
            return result
        except Exception:
            db.rollback()
            if label:
                logger.debug("WriteSerializer: %s rolled back", label)
            raise
        finally:
            db.close()

    def get_metrics(self) -> dict[str, Any]:
        """Return serializer metrics for health/debug endpoints."""
        s = self._stats
        avg_wait = s.total_queue_wait_ms / s.total_writes if s.total_writes else 0
        avg_exec = s.total_exec_ms / s.total_writes if s.total_writes else 0
        return {
            "total_writes": s.total_writes,
            "errors": s.errors,
            "avg_queue_wait_ms": round(avg_wait, 1),
            "max_queue_wait_ms": round(s.max_queue_wait_ms, 1),
            "avg_exec_ms": round(avg_exec, 1),
            "max_exec_ms": round(s.max_exec_ms, 1),
            "label_counts": dict(s._label_counts),
        }


# Process singleton
_serializer = WriteSerializer()


def get_write_serializer() -> WriteSerializer:
    return _serializer
