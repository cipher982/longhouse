"""Supervise the cold archive worker without coupling its lifetime to the API."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from asyncio.subprocess import Process

from zerg.services.archive_worker_status import archive_worker_enabled
from zerg.services.archive_worker_status import write_archive_worker_status

logger = logging.getLogger(__name__)

_supervisor_task: asyncio.Task | None = None
_worker_process: Process | None = None


def _max_backoff_seconds() -> float:
    return max(1.0, float(os.getenv("LONGHOUSE_ARCHIVE_WORKER_MAX_BACKOFF_SECONDS", "30")))


async def _terminate_worker(process: Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _supervise() -> None:
    global _worker_process

    restart_count = 0
    backoff = 1.0
    while True:
        try:
            _worker_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "zerg.services.archive_worker",
                env={**os.environ, "LONGHOUSE_ARCHIVE_WORKER_CHILD": "1"},
            )
            child_started_at = time.monotonic()
            logger.info("Archive worker started pid=%s restart_count=%s", _worker_process.pid, restart_count)
            returncode = await _worker_process.wait()
            if time.monotonic() - child_started_at >= 30.0:
                backoff = 1.0
            restart_count += 1
            write_archive_worker_status(
                {
                    "status": "degraded",
                    "pid": _worker_process.pid,
                    "last_exit_code": returncode,
                    "restart_count": restart_count,
                    "restart_backoff_seconds": backoff,
                }
            )
            logger.error(
                "Archive worker exited returncode=%s restart_count=%s backoff=%.1fs",
                returncode,
                restart_count,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(_max_backoff_seconds(), backoff * 2)
        except asyncio.CancelledError:
            if _worker_process is not None:
                await _terminate_worker(_worker_process)
            raise
        except Exception as exc:
            restart_count += 1
            write_archive_worker_status(
                {
                    "status": "degraded",
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "restart_count": restart_count,
                    "restart_backoff_seconds": backoff,
                }
            )
            logger.exception("Archive worker supervisor failed to start child")
            await asyncio.sleep(backoff)
            backoff = min(_max_backoff_seconds(), backoff * 2)


def start_archive_worker_supervisor() -> None:
    global _supervisor_task
    if not archive_worker_enabled():
        write_archive_worker_status({"status": "disabled"})
        logger.info("Archive worker disabled")
        return
    if _supervisor_task is None or _supervisor_task.done():
        _supervisor_task = asyncio.create_task(_supervise(), name="archive-worker-supervisor")


async def stop_archive_worker_supervisor() -> None:
    global _supervisor_task, _worker_process
    if _supervisor_task is not None and not _supervisor_task.done():
        _supervisor_task.cancel()
        try:
            await _supervisor_task
        except asyncio.CancelledError:
            pass
    elif _worker_process is not None:
        await _terminate_worker(_worker_process)
    _supervisor_task = None
    _worker_process = None
