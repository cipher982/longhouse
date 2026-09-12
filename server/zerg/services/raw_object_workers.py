"""Persistent, lane-isolated process pools for immutable raw-object I/O."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from multiprocessing.connection import wait as wait_for_process_exit
from pathlib import Path
from time import monotonic
from typing import Any

from zerg.config import get_settings
from zerg.storage_v2.media_objects import DecodedMediaObject
from zerg.storage_v2.media_objects import MediaObjectSpec
from zerg.storage_v2.media_objects import SealedMediaObject
from zerg.storage_v2.media_objects import read_media_object
from zerg.storage_v2.media_objects import seal_media_object
from zerg.storage_v2.object_store import FilesystemImmutableObjectStore
from zerg.storage_v2.object_store import ObjectStoreCorruptError
from zerg.storage_v2.object_store import ObjectStoreValidationError
from zerg.storage_v2.raw_objects import MAX_COMPRESSED_BYTES
from zerg.storage_v2.raw_objects import DecodedRawObject
from zerg.storage_v2.raw_objects import RawObjectCorruptError
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import SealedRawObject
from zerg.storage_v2.raw_objects import read_raw_object_from_store
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


def _read_in_worker(root: str, object_path: str, expected_object_hash: str, tenant_id: str) -> DecodedRawObject:
    return read_raw_object_from_store(
        FilesystemImmutableObjectStore(Path(root), tenant_id=tenant_id),
        object_path,
        expected_object_hash=expected_object_hash,
        expected_tenant_id=tenant_id,
    )


def _read_compressed_in_worker(root: str, object_path: str, expected_object_hash: str, tenant_id: str) -> bytes:
    """Return the verified stored bytes of one raw object without decoding it.

    Replication moves objects verbatim. Decoding them into records here would
    cross the process boundary with the whole payload and allocate it a second
    time in the app process, which is what the fetch path exists to avoid.
    """

    store = FilesystemImmutableObjectStore(Path(root), tenant_id=tenant_id)
    try:
        return store.read_verified(
            tenant_id=tenant_id,
            key=object_path,
            sha256=expected_object_hash,
            max_bytes=MAX_COMPRESSED_BYTES,
        )
    except ObjectStoreValidationError as exc:
        raise RawObjectCorruptError(str(exc)) from exc
    except ObjectStoreCorruptError as exc:
        raise RawObjectCorruptError(str(exc)) from exc


def _seal_media_in_worker(root: str, spec: MediaObjectSpec) -> SealedMediaObject:
    return seal_media_object(Path(root), spec)


def _read_media_in_worker(root: str, object_path: str, expected_media_hash: str) -> DecodedMediaObject:
    return read_media_object(Path(root), object_path, expected_media_hash=expected_media_hash)


def _worker_ping() -> int:
    return os.getpid()


_WORKER_TERMINATION_TIMEOUT_SECONDS = 1.0
_WORKER_CANCEL_GRACE_SECONDS = 1.0
_WORKER_CLOSE_TIMEOUT_SECONDS = 3.0


def _executor_processes(executor: ProcessPoolExecutor) -> tuple[tuple[int, multiprocessing.Process], ...]:
    """Capture only the child identities owned by this executor generation."""

    processes = getattr(executor, "_processes", None) or {}
    return tuple((int(pid), process) for pid, process in tuple(processes.items()) if process.pid == pid)


def _terminate_owned_executor(
    executor: ProcessPoolExecutor,
    processes: tuple[tuple[int, multiprocessing.Process], ...],
) -> bool:
    """Prove child exit before entering the executor's shutdown lock."""

    pending = {process.sentinel for _, process in processes}
    for _, process in processes:
        try:
            process.kill()
        except (AssertionError, OSError):
            pass

    deadline = monotonic() + _WORKER_TERMINATION_TIMEOUT_SECONDS
    while pending:
        exited = wait_for_process_exit(pending, timeout=max(0.0, deadline - monotonic()))
        if not exited:
            return False
        pending.difference_update(exited)

    # The manager can hold this lock while joining children. Calling shutdown
    # before killing a stopped child would deadlock a subsequent cleanup attempt.
    executor.shutdown(wait=False, cancel_futures=True)
    return True


class _OwnedProcessPool:
    """Own one executor generation and prove retired children are gone."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.executor = self._new_executor()
        self.retired: dict[ProcessPoolExecutor, tuple[tuple[int, multiprocessing.Process], ...]] = {}
        self._replace_lock = asyncio.Lock()
        self._cleanup_tasks: dict[ProcessPoolExecutor, asyncio.Task[bool]] = {}
        self._closed = False

    def _new_executor(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
        )

    async def retire(self, executor: ProcessPoolExecutor) -> bool:
        async with self._replace_lock:
            if self.executor is executor:
                if not self._closed:
                    self.executor = self._new_executor()
                if executor not in self.retired:
                    self.retired[executor] = _executor_processes(executor)
            elif executor not in self.retired:
                return True
            cleanup = self._cleanup_tasks.get(executor)
            if cleanup is None:
                cleanup = asyncio.create_task(
                    asyncio.to_thread(_terminate_owned_executor, executor, self.retired[executor]),
                    name="terminate-owned-worker-pool",
                )
                self._cleanup_tasks[executor] = cleanup

                def forget(completed: asyncio.Task[bool]) -> None:
                    if self._cleanup_tasks.get(executor) is completed:
                        self._cleanup_tasks.pop(executor, None)
                    if not completed.cancelled() and completed.exception() is None and completed.result():
                        self.retired.pop(executor, None)

                cleanup.add_done_callback(forget)
        return bool(await asyncio.shield(cleanup))

    async def close(self) -> None:
        self._closed = True
        executors = {self.executor, *self.retired}
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(self.retire(executor) for executor in executors)),
                timeout=_WORKER_CLOSE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise RuntimeError("owned worker cleanup exceeded its deadline") from exc
        if not all(results):
            raise RuntimeError("owned worker processes could not be stopped")


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
        self._live_pool = _OwnedProcessPool(live_workers)
        self._repair_pool = _OwnedProcessPool(repair_workers)
        self._user_read_pool = _OwnedProcessPool(user_read_workers)
        self._slot_drainers: set[asyncio.Task[None]] = set()
        self._user_reads: dict[tuple[str, str, str, str, float, float], asyncio.Task[DecodedRawObject]] = {}
        self._closed = False
        self._cleanup_complete = False

    def _pool_for_lane(self, lane: str) -> _OwnedProcessPool:
        if lane == "live":
            return self._live_pool
        if lane == "repair":
            return self._repair_pool
        if lane == "user":
            return self._user_read_pool
        raise ValueError("raw worker lane must be live, repair, or user")

    async def start(self) -> None:
        if self._closed:
            raise RawObjectWorkerError("raw worker pool is closed")
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            loop.run_in_executor(self._live_pool.executor, _worker_ping),
            loop.run_in_executor(self._repair_pool.executor, _worker_ping),
            loop.run_in_executor(self._user_read_pool.executor, _worker_ping),
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
                owner = self._pool_for_lane(lane)
                executor = owner.executor
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
                        await self._replace_executor(lane, executor)
                        raise RawObjectWorkerError(f"media {lane} worker pool crashed twice")
                    await self._replace_executor(lane, executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots, grace_seconds=0.0)
                    raise RawObjectWorkerError(f"media {lane} object seal exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._schedule_abandoned(
                        future,
                        owner,
                        executor,
                        slots,
                        grace_seconds=_WORKER_CANCEL_GRACE_SECONDS,
                    )
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
                owner = self._pool_for_lane(lane)
                executor = owner.executor
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
                        await self._replace_executor(lane, executor)
                        raise RawObjectWorkerError(f"raw {lane} worker pool crashed twice")
                    await self._replace_executor(lane, executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots, grace_seconds=0.0)
                    raise RawObjectWorkerError(f"raw {lane} object seal exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._schedule_abandoned(
                        future,
                        owner,
                        executor,
                        slots,
                        grace_seconds=_WORKER_CANCEL_GRACE_SECONDS,
                    )
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                slots.release()

    async def read(
        self,
        object_path: str,
        expected_object_hash: str,
        tenant_id: str,
        *,
        lane: str = "user",
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 3.0,
    ) -> DecodedRawObject:
        if self._closed:
            raise RawObjectWorkerError("raw worker pool is closed")
        if lane not in {"user", "live", "repair"}:
            raise ValueError("raw read lane must be user, live, or repair")
        key = (lane, object_path, expected_object_hash, tenant_id, queue_timeout_seconds, operation_timeout_seconds)
        task = self._user_reads.get(key)
        if task is None:
            task = asyncio.create_task(
                self._read_with_recovery(
                    object_path,
                    expected_object_hash,
                    tenant_id,
                    lane=lane,
                    queue_timeout_seconds=queue_timeout_seconds,
                    operation_timeout_seconds=operation_timeout_seconds,
                ),
                name=f"raw-{lane}-object-read",
            )
            self._user_reads[key] = task

            def forget(completed: asyncio.Task[DecodedRawObject]) -> None:
                if self._user_reads.get(key) is completed:
                    self._user_reads.pop(key, None)
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(forget)
        return await asyncio.shield(task)

    async def _read_with_recovery(
        self,
        object_path: str,
        expected_object_hash: str,
        tenant_id: str,
        *,
        lane: str,
        queue_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> DecodedRawObject:
        owner = self._pool_for_lane(lane)
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                slots = self._user_read_slots if lane == "user" else self._live_slots if lane == "live" else self._repair_slots
                await slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy(f"raw {lane} read queue is full") from exc
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
                        tenant_id,
                    )
                    async with asyncio.timeout(operation_timeout_seconds):
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    if attempt:
                        await self._replace_executor(lane, executor)
                        raise RawObjectWorkerError("raw user reader pool crashed twice")
                    await self._replace_executor(lane, executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots, grace_seconds=0.0)
                    raise RawObjectWorkerError(f"raw {lane} read exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._schedule_abandoned(
                        future,
                        owner,
                        executor,
                        slots,
                        grace_seconds=_WORKER_CANCEL_GRACE_SECONDS,
                    )
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                slots.release()

    async def read_media(
        self,
        object_path: str,
        expected_media_hash: str,
        *,
        lane: str = "user",
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 3.0,
    ) -> DecodedMediaObject:
        """Read and hash-verify media on the selected worker lane."""

        if self._closed:
            raise RawObjectWorkerError("storage worker pool is closed")
        if lane not in {"user", "live", "repair"}:
            raise ValueError("media read lane must be user, live, or repair")
        owner = self._pool_for_lane(lane)
        slots = self._user_read_slots if lane == "user" else self._live_slots if lane == "live" else self._repair_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy(f"media {lane} read queue is full") from exc
        release_slot = True
        try:
            for attempt in range(2):
                executor = owner.executor
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
                        await self._replace_executor(lane, executor)
                        raise RawObjectWorkerError("media user reader pool crashed twice")
                    await self._replace_executor(lane, executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots, grace_seconds=0.0)
                    raise RawObjectWorkerError(f"media {lane} read exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._schedule_abandoned(
                        future,
                        owner,
                        executor,
                        slots,
                        grace_seconds=_WORKER_CANCEL_GRACE_SECONDS,
                    )
                    raise
            raise AssertionError("unreachable")
        finally:
            if release_slot:
                slots.release()

    async def read_verified_compressed(
        self,
        object_path: str,
        expected_object_hash: str,
        tenant_id: str,
        *,
        lane: str = "background",
        queue_timeout_seconds: float = 0.25,
        operation_timeout_seconds: float = 10.0,
    ) -> bytes:
        """Read and hash-verify one stored object without decoding it.

        Defaults to the background lane so replication cannot fill the
        user-read queue: that lane is one worker wide and serves session
        detail and export, so a replica sweep must never turn an ordinary raw
        read into a 503.
        """

        if self._closed:
            raise RawObjectWorkerError("raw worker pool is closed")
        if lane not in {"background", "user"}:
            raise ValueError("compressed read lane must be background or user")
        background = lane == "background"
        owner = self._repair_pool if background else self._user_read_pool
        slots = self._repair_slots if background else self._user_read_slots
        try:
            async with asyncio.timeout(queue_timeout_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise RawObjectWorkerBusy("raw object read queue is full") from exc
        release_slot = True
        try:
            for attempt in range(2):
                executor = owner.executor
                try:
                    future = asyncio.get_running_loop().run_in_executor(
                        executor,
                        _read_compressed_in_worker,
                        str(self.root),
                        object_path,
                        expected_object_hash,
                        tenant_id,
                    )
                    async with asyncio.timeout(operation_timeout_seconds):
                        return await asyncio.shield(future)
                except BrokenProcessPool:
                    if attempt:
                        await self._replace_executor("repair" if background else "user", executor)
                        raise RawObjectWorkerError("raw object reader pool crashed twice")
                    await self._replace_executor("repair" if background else "user", executor)
                except TimeoutError as exc:
                    release_slot = False
                    self._schedule_abandoned(future, owner, executor, slots, grace_seconds=0.0)
                    raise RawObjectWorkerError("raw object read exceeded its deadline") from exc
                except asyncio.CancelledError:
                    release_slot = False
                    self._schedule_abandoned(
                        future,
                        owner,
                        executor,
                        slots,
                        grace_seconds=_WORKER_CANCEL_GRACE_SECONDS,
                    )
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
        *,
        grace_seconds: float,
    ) -> None:
        async def drain() -> None:
            release_slot = False
            try:
                if not future.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(future), timeout=grace_seconds)
                    except TimeoutError:
                        pass
                    except BaseException:
                        release_slot = True
                if not future.done() and not release_slot:
                    release_slot = await owner.retire(executor)
                    if future.done():
                        try:
                            future.exception()
                        except BaseException:
                            pass
                elif future.done():
                    try:
                        future.exception()
                    except BaseException:
                        pass
                    release_slot = True
            finally:
                if release_slot:
                    slots.release()

        task = asyncio.create_task(drain())
        self._slot_drainers.add(task)
        task.add_done_callback(self._slot_drainers.discard)

    async def _replace_executor(self, lane: str, broken: ProcessPoolExecutor) -> bool:
        return await self._pool_for_lane(lane).retire(broken)

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
        await asyncio.gather(
            self._live_pool.close(),
            self._repair_pool.close(),
            self._user_read_pool.close(),
        )
        self._cleanup_complete = True


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
