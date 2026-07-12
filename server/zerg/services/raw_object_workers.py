"""Persistent, lane-isolated process pools for immutable raw-object I/O."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from zerg.config import get_settings
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import SealedRawObject
from zerg.storage_v2.raw_objects import seal_raw_object


class RawObjectWorkerError(RuntimeError):
    pass


class RawObjectWorkerBusy(RawObjectWorkerError):
    pass


def storage_v2_root() -> Path:
    override = os.getenv("LONGHOUSE_STORAGE_V2_ROOT")
    if override:
        return Path(override).expanduser()
    return get_settings().data_dir / "objects-v2"


def _seal_in_worker(root: str, spec: RawObjectSpec) -> SealedRawObject:
    return seal_raw_object(Path(root), spec)


def _worker_ping() -> int:
    return os.getpid()


class RawObjectWorkerPool:
    """Bounded persistent workers with capacity reserved for live ingest."""

    def __init__(
        self,
        root: Path,
        *,
        live_workers: int = 2,
        repair_workers: int = 1,
        queue_multiplier: int = 2,
    ) -> None:
        if live_workers < 1 or repair_workers < 1 or queue_multiplier < 1:
            raise ValueError("raw worker counts and queue multiplier must be positive")
        self.root = root.expanduser().resolve()
        self.live_workers = live_workers
        self.repair_workers = repair_workers
        self._live_slots = asyncio.Semaphore(live_workers * queue_multiplier)
        self._repair_slots = asyncio.Semaphore(repair_workers * queue_multiplier)
        self._live_executor = self._new_executor(live_workers)
        self._repair_executor = self._new_executor(repair_workers)
        self._replace_lock = asyncio.Lock()
        self._slot_drainers: set[asyncio.Task[None]] = set()
        self._closed = False

    @staticmethod
    def _new_executor(workers: int) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
        )

    async def start(self) -> None:
        if self._closed:
            raise RawObjectWorkerError("raw worker pool is closed")
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            loop.run_in_executor(self._live_executor, _worker_ping),
            loop.run_in_executor(self._repair_executor, _worker_ping),
        )

    async def seal(
        self,
        spec: RawObjectSpec,
        *,
        lane: str,
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 10.0,
    ) -> SealedRawObject:
        if self._closed:
            raise RawObjectWorkerError("raw worker pool is closed")
        if lane not in {"live", "repair"}:
            raise ValueError("raw worker lane must be live or repair")
        if queue_timeout_seconds <= 0 or operation_timeout_seconds <= 0:
            raise ValueError("raw worker deadlines must be positive")
        slots = self._live_slots if lane == "live" else self._repair_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy(f"raw {lane} worker queue is full") from exc
        return await self._seal_once_with_recovery(
            spec,
            lane=lane,
            timeout_seconds=operation_timeout_seconds,
            slots=slots,
        )

    async def _seal_once_with_recovery(
        self,
        spec: RawObjectSpec,
        *,
        lane: str,
        timeout_seconds: float,
        slots: asyncio.Semaphore,
    ) -> SealedRawObject:
        release_slot = True
        try:
            for attempt in range(2):
                executor = self._live_executor if lane == "live" else self._repair_executor
                try:
                    future = asyncio.get_running_loop().run_in_executor(
                        executor,
                        _seal_in_worker,
                        str(self.root),
                        spec,
                    )
                    async with asyncio.timeout(timeout_seconds):
                        # A timeout or caller cancellation must not free queue
                        # capacity while the process is still sealing the file.
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    if attempt:
                        raise RawObjectWorkerError(f"raw {lane} worker pool crashed twice")
                    await self._replace_executor(lane, executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._drain_slot_when_done(future, slots)
                    raise RawObjectWorkerError(f"raw {lane} object seal exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._drain_slot_when_done(future, slots)
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                slots.release()

    def _drain_slot_when_done(self, future: asyncio.Future[Any], slots: asyncio.Semaphore) -> None:
        async def drain() -> None:
            try:
                await asyncio.shield(future)
            except BaseException:
                pass
            finally:
                slots.release()

        task = asyncio.create_task(drain())
        self._slot_drainers.add(task)
        task.add_done_callback(self._slot_drainers.discard)

    async def _replace_executor(self, lane: str, broken: ProcessPoolExecutor) -> None:
        async with self._replace_lock:
            if lane == "live":
                if self._live_executor is not broken:
                    return
                old = self._live_executor
                self._live_executor = self._new_executor(self.live_workers)
            else:
                if self._repair_executor is not broken:
                    return
                old = self._repair_executor
                self._repair_executor = self._new_executor(self.repair_workers)
            old.shutdown(wait=False, cancel_futures=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            asyncio.to_thread(self._live_executor.shutdown, wait=True, cancel_futures=True),
            asyncio.to_thread(self._repair_executor.shutdown, wait=True, cancel_futures=True),
        )
        if self._slot_drainers:
            await asyncio.gather(*tuple(self._slot_drainers), return_exceptions=True)


_pool: RawObjectWorkerPool | None = None


def get_raw_object_worker_pool() -> RawObjectWorkerPool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = RawObjectWorkerPool(storage_v2_root())
    return _pool


async def close_raw_object_worker_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


__all__ = [
    "RawObjectWorkerBusy",
    "RawObjectWorkerError",
    "RawObjectWorkerPool",
    "close_raw_object_worker_pool",
    "get_raw_object_worker_pool",
    "storage_v2_root",
]
