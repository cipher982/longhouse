"""Persistent lane-isolated workers for immutable render objects."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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


def _seal_in_worker(root: str, spec: RenderObjectSpec) -> SealedRenderObject:
    return seal_render_object(Path(root), spec)


def _read_in_worker(root: str, object_path: str, expected_object_hash: str) -> DecodedRenderObject:
    return read_render_object(Path(root), object_path, expected_object_hash=expected_object_hash)


def _worker_ping() -> int:
    return os.getpid()


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
        self._live_executor = self._new_executor(live_workers)
        self._repair_executor = self._new_executor(repair_workers)
        self._user_read_executor = self._new_executor(user_read_workers)
        self._replace_lock = asyncio.Lock()
        self._slot_drainers: set[asyncio.Task[None]] = set()
        self._closed = False

    @staticmethod
    def _new_executor(workers: int) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))

    async def start(self) -> None:
        if self._closed:
            raise RenderObjectWorkerError("render worker pool is closed")
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            loop.run_in_executor(self._live_executor, _worker_ping),
            loop.run_in_executor(self._repair_executor, _worker_ping),
            loop.run_in_executor(self._user_read_executor, _worker_ping),
        )

    @asynccontextmanager
    async def admission(self, lane: str, *, queue_timeout_seconds: float = 0.25) -> AsyncIterator[None]:
        if self._closed:
            raise RenderObjectWorkerError("render worker pool is closed")
        if lane not in {"live", "repair"}:
            raise ValueError("render worker lane must be live or repair")
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
        slots = self._live_slots if lane == "live" else self._repair_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RenderObjectWorkerBusy(f"render {lane} worker queue is full") from exc
        return await self._seal_with_recovery(spec, lane=lane, timeout_seconds=operation_timeout_seconds, slots=slots)

    async def _seal_with_recovery(
        self,
        spec: RenderObjectSpec,
        *,
        lane: str,
        timeout_seconds: float,
        slots: asyncio.Semaphore,
    ) -> SealedRenderObject:
        release_slot = True
        try:
            for attempt in range(2):
                executor = self._live_executor if lane == "live" else self._repair_executor
                try:
                    future = asyncio.get_running_loop().run_in_executor(executor, _seal_in_worker, str(self.root), spec)
                    async with asyncio.timeout(timeout_seconds):
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    if attempt:
                        raise RenderObjectWorkerError(f"render {lane} worker pool crashed twice")
                    await self._replace_executor(lane, executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._drain_slot_when_done(future, slots)
                    raise RenderObjectWorkerError(f"render {lane} object seal exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._drain_slot_when_done(future, slots)
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
        slots = self._user_read_slots if lane == "user" else self._repair_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RenderObjectWorkerBusy(f"render {lane} read queue is full") from exc
        release_slot = True
        try:
            for attempt in range(2):
                executor = self._user_read_executor if lane == "user" else self._repair_executor
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
                    if attempt:
                        raise RenderObjectWorkerError(f"render {lane} reader pool crashed twice")
                    await self._replace_executor("user" if lane == "user" else "repair", executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._drain_slot_when_done(future, slots)
                    raise RenderObjectWorkerError(f"render {lane} read exceeded its deadline") from exc
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
            if lane == "user":
                if self._user_read_executor is not broken:
                    return
                old = self._user_read_executor
                self._user_read_executor = self._new_executor(self.user_read_workers)
            elif lane == "live":
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
            asyncio.to_thread(self._user_read_executor.shutdown, wait=True, cancel_futures=True),
        )
        if self._slot_drainers:
            await asyncio.gather(*tuple(self._slot_drainers), return_exceptions=True)


_pool: RenderObjectWorkerPool | None = None


def get_render_object_worker_pool() -> RenderObjectWorkerPool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = RenderObjectWorkerPool(storage_v2_root())
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
