"""Persistent lane-isolated workers for immutable render objects."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from zerg.services.raw_object_workers import _WORKER_CLOSE_TIMEOUT_SECONDS
from zerg.services.raw_object_workers import _future_completed_normally
from zerg.services.raw_object_workers import _OwnedProcessPool
from zerg.services.raw_object_workers import storage_v2_root
from zerg.storage_v2.render_objects import DecodedRenderObject
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import SealedRenderObject
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object


class RenderObjectWorkerError(RuntimeError):
    pass


class RenderObjectWorkerBusy(RenderObjectWorkerError):
    pass


logger = logging.getLogger(__name__)


def _seal_in_worker(root: str, spec: RenderObjectSpec) -> SealedRenderObject:
    return seal_render_object(Path(root), spec)


def _read_in_worker(root: str, object_path: str, expected_object_hash: str) -> DecodedRenderObject:
    return read_render_object(Path(root), object_path, expected_object_hash=expected_object_hash)


def _worker_ping() -> int:
    return os.getpid()


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, value)


class RenderObjectWorkerPool:
    def __init__(
        self,
        root: Path,
        *,
        live_workers: int = 1,
        repair_workers: int = 1,
        user_read_workers: int = 2,
        queue_multiplier: int = 2,
    ) -> None:
        if live_workers < 1 or repair_workers < 1 or user_read_workers < 1 or queue_multiplier < 1:
            raise ValueError("render worker counts and queue multiplier must be positive")
        self.root = root.expanduser().resolve()
        self.live_workers = live_workers
        self.repair_workers = repair_workers
        self.user_read_workers = user_read_workers
        self._live_slots = asyncio.Semaphore(live_workers * queue_multiplier)
        self._repair_slots = asyncio.Semaphore(repair_workers * queue_multiplier)
        self._user_read_slots = asyncio.Semaphore(user_read_workers * queue_multiplier)
        self._live_admission_slots = asyncio.Semaphore(live_workers * queue_multiplier)
        self._repair_admission_slots = asyncio.Semaphore(repair_workers * queue_multiplier)
        self._live_pool = _OwnedProcessPool(live_workers)
        self._repair_pool = _OwnedProcessPool(repair_workers)
        self._user_read_pool = _OwnedProcessPool(user_read_workers)
        self._slot_drainers: set[asyncio.Task[None]] = set()
        self._user_reads: dict[tuple[str, str, str, float, float], asyncio.Task[DecodedRenderObject]] = {}
        self._closed = False
        self._cleanup_complete = False

    def _pool_for_lane(self, lane: str) -> _OwnedProcessPool:
        if lane == "live":
            return self._live_pool
        if lane in {"repair", "background"}:
            return self._repair_pool
        if lane == "user":
            return self._user_read_pool
        raise ValueError("render worker lane must be user, background, live, or repair")

    async def start(self) -> None:
        if self._closed:
            raise RenderObjectWorkerError("render worker pool is closed")
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            loop.run_in_executor(self._live_pool.executor, _worker_ping),
            loop.run_in_executor(self._repair_pool.executor, _worker_ping),
            loop.run_in_executor(self._user_read_pool.executor, _worker_ping),
        )

    async def _retire_broken_executor(
        self,
        lane: str,
        owner: _OwnedProcessPool,
        executor: ProcessPoolExecutor,
        slots: asyncio.Semaphore,
        *,
        queue_timeout_seconds: float,
    ) -> None:
        owner.defer_slot(executor, slots)
        if not await owner.retire(executor):
            raise RenderObjectWorkerBusy(f"render {lane} workers are unavailable while child cleanup is pending")
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RenderObjectWorkerBusy(f"render {lane} worker retry queue is full") from exc

    @asynccontextmanager
    async def admission(self, lane: str, *, queue_timeout_seconds: float = 0.25) -> AsyncIterator[None]:
        if self._closed:
            raise RenderObjectWorkerError("render worker pool is closed")
        if lane not in {"live", "repair"}:
            raise ValueError("render worker lane must be live or repair")
        owner = self._pool_for_lane(lane)
        if not await owner.recover():
            raise RenderObjectWorkerBusy(f"render {lane} workers are unavailable while child cleanup is pending")
        slots = self._live_admission_slots if lane == "live" else self._repair_admission_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RenderObjectWorkerBusy(f"render {lane} admission queue is full") from exc
        try:
            yield
        finally:
            slots.release()

    async def seal(
        self,
        spec: RenderObjectSpec,
        *,
        lane: str,
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 3.0,
    ) -> SealedRenderObject:
        if self._closed:
            raise RenderObjectWorkerError("render worker pool is closed")
        if lane not in {"live", "repair"}:
            raise ValueError("render worker lane must be live or repair")
        owner = self._pool_for_lane(lane)
        if not await owner.recover():
            raise RenderObjectWorkerBusy(f"render {lane} workers are unavailable while child cleanup is pending")
        slots = self._live_slots if lane == "live" else self._repair_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RenderObjectWorkerBusy(f"render {lane} worker queue is full") from exc
        return await self._seal_with_recovery(
            spec,
            lane=lane,
            queue_timeout_seconds=queue_timeout_seconds,
            timeout_seconds=operation_timeout_seconds,
            slots=slots,
        )

    async def _seal_with_recovery(
        self,
        spec: RenderObjectSpec,
        *,
        lane: str,
        queue_timeout_seconds: float,
        timeout_seconds: float,
        slots: asyncio.Semaphore,
    ) -> SealedRenderObject:
        release_slot = True
        try:
            for attempt in range(2):
                owner = self._pool_for_lane(lane)
                executor = owner.executor
                try:
                    future = asyncio.get_running_loop().run_in_executor(executor, _seal_in_worker, str(self.root), spec)
                    async with asyncio.timeout(timeout_seconds):
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    release_slot = False
                    await self._retire_broken_executor(
                        lane,
                        owner,
                        executor,
                        slots,
                        queue_timeout_seconds=queue_timeout_seconds,
                    )
                    release_slot = True
                    if attempt:
                        raise RenderObjectWorkerError(f"render {lane} worker pool crashed twice")
                except TimeoutError as exc:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots)
                    raise RenderObjectWorkerError(f"render {lane} object seal exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots)
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                slots.release()

    async def read(
        self,
        object_path: str,
        expected_object_hash: str,
        *,
        lane: str = "user",
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 3.0,
    ) -> DecodedRenderObject:
        if self._closed:
            raise RenderObjectWorkerError("render worker pool is closed")
        if lane not in {"user", "background"}:
            raise ValueError("render read lane must be user or background")
        if lane == "user":
            key = (lane, object_path, expected_object_hash, queue_timeout_seconds, operation_timeout_seconds)
            task = self._user_reads.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._read_with_recovery(
                        object_path,
                        expected_object_hash,
                        lane=lane,
                        queue_timeout_seconds=queue_timeout_seconds,
                        operation_timeout_seconds=operation_timeout_seconds,
                    ),
                    name="render-user-object-read",
                )
                self._user_reads[key] = task

                def forget(completed: asyncio.Task[DecodedRenderObject]) -> None:
                    if self._user_reads.get(key) is completed:
                        self._user_reads.pop(key, None)
                    if not completed.cancelled():
                        completed.exception()

                task.add_done_callback(forget)
            return await asyncio.shield(task)
        return await self._read_with_recovery(
            object_path,
            expected_object_hash,
            lane=lane,
            queue_timeout_seconds=queue_timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )

    async def _read_with_recovery(
        self,
        object_path: str,
        expected_object_hash: str,
        *,
        lane: str,
        queue_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> DecodedRenderObject:
        owner = self._pool_for_lane(lane)
        if not await owner.recover():
            raise RenderObjectWorkerBusy(f"render {lane} workers are unavailable while child cleanup is pending")
        slots = self._user_read_slots if lane == "user" else self._repair_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RenderObjectWorkerBusy(f"render {lane} read queue is full") from exc
        release_slot = True
        try:
            for attempt in range(2):
                executor = owner.executor
                try:
                    future = asyncio.get_running_loop().run_in_executor(
                        executor,
                        _read_in_worker,
                        str(self.root),
                        object_path,
                        expected_object_hash,
                    )
                    async with asyncio.timeout(operation_timeout_seconds):
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    release_slot = False
                    await self._retire_broken_executor(
                        "user" if lane == "user" else "repair",
                        owner,
                        executor,
                        slots,
                        queue_timeout_seconds=queue_timeout_seconds,
                    )
                    release_slot = True
                    if attempt:
                        raise RenderObjectWorkerError(f"render {lane} reader pool crashed twice")
                except TimeoutError as exc:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots)
                    raise RenderObjectWorkerError(f"render {lane} read exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots)
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                slots.release()

    def _schedule_abandoned(
        self,
        future: asyncio.Future[Any],
        owner: _OwnedProcessPool,
        executor: ProcessPoolExecutor,
        slots: asyncio.Semaphore,
    ) -> None:
        if _future_completed_normally(future):
            slots.release()
            return
        owner.defer_slot(executor, slots)
        future.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)

        async def drain() -> None:
            try:
                if not await owner.retire(executor):
                    logger.warning("render abandoned worker cleanup remains unavailable")
            except asyncio.CancelledError:
                raise
            except BaseException:
                logger.exception("render abandoned worker cleanup failed")

        task = asyncio.create_task(drain())
        self._slot_drainers.add(task)
        task.add_done_callback(self._slot_drainers.discard)

    async def close(self) -> None:
        if self._cleanup_complete:
            return
        self._closed = True
        for task in tuple(self._user_reads.values()):
            task.cancel()
        if self._user_reads:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._user_reads.values()), return_exceptions=True),
                    timeout=_WORKER_CLOSE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                pass
        for task in tuple(self._slot_drainers):
            task.cancel()
        if self._slot_drainers:
            await asyncio.gather(*tuple(self._slot_drainers), return_exceptions=True)
        results = await asyncio.gather(
            self._live_pool.close(),
            self._repair_pool.close(),
            self._user_read_pool.close(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        self._cleanup_complete = True


_pool: RenderObjectWorkerPool | None = None


def get_render_object_worker_pool() -> RenderObjectWorkerPool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = RenderObjectWorkerPool(
            storage_v2_root(),
            live_workers=_env_positive_int("LONGHOUSE_STORAGE_RENDER_LIVE_WORKERS", 1),
            repair_workers=_env_positive_int("LONGHOUSE_STORAGE_RENDER_REPAIR_WORKERS", 1),
            user_read_workers=_env_positive_int("LONGHOUSE_STORAGE_RENDER_READ_WORKERS", 2),
            queue_multiplier=_env_positive_int("LONGHOUSE_STORAGE_RENDER_QUEUE_MULTIPLIER", 2),
        )
    return _pool


async def close_render_object_worker_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


__all__ = [
    "RenderObjectWorkerBusy",
    "RenderObjectWorkerError",
    "RenderObjectWorkerPool",
    "close_render_object_worker_pool",
    "get_render_object_worker_pool",
]
