"""Live worker output buffer for streaming runner_exec chunks.

This is a volatile in-memory tail buffer keyed by worker_id. It is designed
for low-latency "peeking" without persisting every chunk to the database.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque
from typing import Dict
from typing import Optional

# Max bytes to retain per worker (tail buffer)
DEFAULT_MAX_BYTES = 50 * 1024
# Drop buffers after inactivity to avoid leaks
DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6 hours


@dataclass
class WorkerOutputMeta:
    """Metadata cached for a worker output buffer."""

    job_id: Optional[int] = None
    run_id: Optional[int] = None
    trace_id: Optional[str] = None
    owner_id: Optional[int] = None
    last_resolved_at: float = 0


class _OutputBuffer:
    """Thread-safe tail buffer for a single worker."""

    def __init__(self, max_bytes: int) -> None:
        self._chunks: Deque[str] = deque()
        self._size = 0
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self.updated_at = time.time()
        self.last_runner_job_id: Optional[str] = None
        self.meta = WorkerOutputMeta()

    def append(self, data: str) -> None:
        if not data:
            return

        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            self.updated_at = time.time()

            # Trim to max bytes (tail)
            overflow = self._size - self._max_bytes
            while overflow > 0 and self._chunks:
                oldest = self._chunks[0]
                if overflow >= len(oldest):
                    self._chunks.popleft()
                    self._size -= len(oldest)
                    overflow -= len(oldest)
                else:
                    # Trim only the overflow from the oldest chunk
                    self._chunks[0] = oldest[overflow:]
                    self._size -= overflow
                    overflow = 0

    def get_tail(self, max_bytes: int | None = None) -> str:
        with self._lock:
            if not self._chunks:
                return ""
            combined = "".join(self._chunks)

        if not max_bytes or max_bytes <= 0:
            return combined

        if len(combined) <= max_bytes:
            return combined

        return combined[-max_bytes:]

    def set_meta(
        self,
        *,
        job_id: Optional[int] = None,
        run_id: Optional[int] = None,
        trace_id: Optional[str] = None,
        owner_id: Optional[int] = None,
        resolved: bool = False,
    ) -> None:
        if job_id is not None:
            self.meta.job_id = job_id
        if run_id is not None:
            self.meta.run_id = run_id
        if trace_id is not None:
            self.meta.trace_id = trace_id
        if owner_id is not None:
            self.meta.owner_id = owner_id
        if resolved:
            self.meta.last_resolved_at = time.time()


class WorkerOutputBuffer:
    """Singleton store for live worker output buffers."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_seconds
        self._buffers: Dict[str, _OutputBuffer] = {}
        self._lock = threading.Lock()

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [worker_id for worker_id, buf in self._buffers.items() if now - buf.updated_at > self._ttl_seconds]
        for worker_id in stale:
            self._buffers.pop(worker_id, None)

    def _get_or_create(self, worker_id: str) -> _OutputBuffer:
        with self._lock:
            self._prune_locked()
            buf = self._buffers.get(worker_id)
            if buf is None:
                buf = _OutputBuffer(self._max_bytes)
                self._buffers[worker_id] = buf
            return buf

    def append_output(
        self,
        *,
        worker_id: str,
        stream: str,
        data: str,
        runner_job_id: Optional[str] = None,
        job_id: Optional[int] = None,
        run_id: Optional[int] = None,
        trace_id: Optional[str] = None,
        owner_id: Optional[int] = None,
        resolved: bool = False,
    ) -> None:
        """Append output chunk for a worker."""
        if not worker_id:
            return

        buf = self._get_or_create(worker_id)
        buf.set_meta(
            job_id=job_id,
            run_id=run_id,
            trace_id=trace_id,
            owner_id=owner_id,
            resolved=resolved,
        )

        if not data:
            return

        prefix = ""
        if runner_job_id and runner_job_id != buf.last_runner_job_id:
            prefix = f"\n\n[runner_job {runner_job_id}]\n"
            buf.last_runner_job_id = runner_job_id
        if stream == "stderr":
            prefix += "[stderr] "

        buf.append(prefix + data)

    def get_tail(self, worker_id: str, *, max_bytes: int | None = None) -> str:
        """Get tail output for a worker."""
        with self._lock:
            self._prune_locked()
            buf = self._buffers.get(worker_id)
        if not buf:
            return ""
        return buf.get_tail(max_bytes)

    def get_meta(self, worker_id: str) -> WorkerOutputMeta | None:
        """Return cached metadata for a worker output buffer."""
        with self._lock:
            self._prune_locked()
            buf = self._buffers.get(worker_id)
        if not buf:
            return None
        return buf.meta


_OUTPUT_BUFFER: WorkerOutputBuffer | None = None


def get_worker_output_buffer() -> WorkerOutputBuffer:
    """Get singleton worker output buffer."""
    global _OUTPUT_BUFFER
    if _OUTPUT_BUFFER is None:
        _OUTPUT_BUFFER = WorkerOutputBuffer()
    return _OUTPUT_BUFFER


__all__ = [
    "WorkerOutputBuffer",
    "WorkerOutputMeta",
    "get_worker_output_buffer",
]
