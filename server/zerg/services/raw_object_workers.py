"""Persistent, lane-isolated process pools for immutable raw-object I/O."""

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

from zerg.config import get_settings
from zerg.storage_v2.media_objects import DecodedMediaObject
from zerg.storage_v2.media_objects import MediaObjectSpec
from zerg.storage_v2.media_objects import SealedMediaObject
from zerg.storage_v2.media_objects import read_media_object
from zerg.storage_v2.media_objects import seal_media_object
from zerg.storage_v2.raw_objects import DecodedRawObject
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import SealedRawObject
from zerg.storage_v2.raw_objects import read_raw_object
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


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, value)


def _seal_in_worker(root: str, spec: RawObjectSpec) -> SealedRawObject:
    return seal_raw_object(Path(root), spec)


def _read_in_worker(root: str, object_path: str, expected_object_hash: str) -> DecodedRawObject:
    return read_raw_object(Path(root), object_path, expected_object_hash=expected_object_hash)


def _seal_media_in_worker(root: str, spec: MediaObjectSpec) -> SealedMediaObject:
    return seal_media_object(Path(root), spec)


def _read_media_in_worker(root: str, object_path: str, expected_media_hash: str) -> DecodedMediaObject:
    return read_media_object(Path(root), object_path, expected_media_hash=expected_media_hash)


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
        user_read_workers: int = 1,
        queue_multiplier: int = 2,
    ) -> None:
        if live_workers < 1 or repair_workers < 1 or user_read_workers < 1 or queue_multiplier < 1:
            raise ValueError("raw worker counts and queue multiplier must be positive")
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
            loop.run_in_executor(self._user_read_executor, _worker_ping),
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

    async def seal_media(
        self,
        spec: MediaObjectSpec,
        *,
        lane: str,
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 15.0,
    ) -> SealedMediaObject:
        """Seal media through the same bounded live/repair storage lanes."""

        if self._closed:
            raise RawObjectWorkerError("storage worker pool is closed")
        if lane not in {"live", "repair"}:
            raise ValueError("media worker lane must be live or repair")
        if queue_timeout_seconds <= 0 or operation_timeout_seconds <= 0:
            raise ValueError("media worker deadlines must be positive")
        slots = self._live_slots if lane == "live" else self._repair_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy(f"media {lane} worker queue is full") from exc
        release_slot = True
        try:
            for attempt in range(2):
                executor = self._live_executor if lane == "live" else self._repair_executor
                try:
                    future = asyncio.get_running_loop().run_in_executor(
                        executor,
                        _seal_media_in_worker,
                        str(self.root),
                        spec,
                    )
                    async with asyncio.timeout(operation_timeout_seconds):
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    if attempt:
                        raise RawObjectWorkerError(f"media {lane} worker pool crashed twice")
                    await self._replace_executor(lane, executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._drain_slot_when_done(future, slots)
                    raise RawObjectWorkerError(f"media {lane} object seal exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._drain_slot_when_done(future, slots)
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                slots.release()

    @asynccontextmanager
    async def admission(
        self,
        lane: str,
        *,
        queue_timeout_seconds: float = 0.25,
    ) -> AsyncIterator[None]:
        """Reserve bounded request capacity before JSON/base64 decoding."""

        if self._closed:
            raise RawObjectWorkerError("raw worker pool is closed")
        if lane not in {"live", "repair"}:
            raise ValueError("raw worker lane must be live or repair")
        if queue_timeout_seconds <= 0:
            raise ValueError("raw worker queue deadline must be positive")
        slots = self._live_admission_slots if lane == "live" else self._repair_admission_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy(f"raw {lane} admission queue is full") from exc
        try:
            yield
        finally:
            slots.release()

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

    async def read(
        self,
        object_path: str,
        expected_object_hash: str,
        *,
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 3.0,
    ) -> DecodedRawObject:
        if self._closed:
            raise RawObjectWorkerError("raw worker pool is closed")
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await self._user_read_slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy("raw user read queue is full") from exc
        release_slot = True
        try:
            for attempt in range(2):
                executor = self._user_read_executor
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
                        raise RawObjectWorkerError("raw user reader pool crashed twice")
                    await self._replace_executor("user", executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._drain_slot_when_done(future, self._user_read_slots)
                    raise RawObjectWorkerError("raw user read exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._drain_slot_when_done(future, self._user_read_slots)
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                self._user_read_slots.release()

    async def read_media(
        self,
        object_path: str,
        expected_media_hash: str,
        *,
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 3.0,
    ) -> DecodedMediaObject:
        """Read and hash-verify media on the reserved user-read lane."""

        if self._closed:
            raise RawObjectWorkerError("storage worker pool is closed")
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await self._user_read_slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy("media user read queue is full") from exc
        release_slot = True
        try:
            for attempt in range(2):
                executor = self._user_read_executor
                try:
                    future = asyncio.get_running_loop().run_in_executor(
                        executor,
                        _read_media_in_worker,
                        str(self.root),
                        object_path,
                        expected_media_hash,
                    )
                    async with asyncio.timeout(operation_timeout_seconds):
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    if attempt:
                        raise RawObjectWorkerError("media user reader pool crashed twice")
                    await self._replace_executor("user", executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._drain_slot_when_done(future, self._user_read_slots)
                    raise RawObjectWorkerError("media user read exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._drain_slot_when_done(future, self._user_read_slots)
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                self._user_read_slots.release()

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


_pool: RawObjectWorkerPool | None = None


def get_raw_object_worker_pool() -> RawObjectWorkerPool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = RawObjectWorkerPool(
            storage_v2_root(),
            live_workers=_env_positive_int("LONGHOUSE_STORAGE_RAW_LIVE_WORKERS", 2),
            repair_workers=_env_positive_int("LONGHOUSE_STORAGE_RAW_REPAIR_WORKERS", 1),
            user_read_workers=_env_positive_int("LONGHOUSE_STORAGE_RAW_READ_WORKERS", 1),
            queue_multiplier=_env_positive_int("LONGHOUSE_STORAGE_RAW_QUEUE_MULTIPLIER", 2),
        )
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
