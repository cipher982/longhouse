"""One owner for a PTY output reader, its wake pipe, and retained recording."""

from __future__ import annotations

import errno
import json
import os
import select
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_TAIL_BYTES = 64 * 1024
_POLL_SECONDS = 0.2
_LOCK_TIMEOUT_SECONDS = 1.0
_JOIN_RETRY_SECONDS = 0.2


@dataclass(frozen=True)
class TimedChunk:
    """One read from the PTY master: when it returned (``time.monotonic()``) and what it held."""

    t: float
    offset: int
    data: bytes


def load_timed_chunks(recording_path: Path, timing_path: Path) -> list[TimedChunk]:
    """Re-join a recording with its timing sidecar (one JSON line per chunk after a header)."""

    raw = recording_path.read_bytes()
    chunks: list[TimedChunk] = []
    with timing_path.open("r", encoding="utf-8") as lines:
        header = json.loads(lines.readline())
        if header.get("clock") != "monotonic":
            raise ValueError(f"unknown PTY timing clock in {timing_path}")
        for line in lines:
            entry = json.loads(line)
            start, size = int(entry["o"]), int(entry["n"])
            if start + size > len(raw):
                raise ValueError(f"PTY timing sidecar runs past the recording at offset {start}")
            chunks.append(TimedChunk(float(entry["t"]), start, raw[start : start + size]))
    return chunks


class OwnedPtyReader:
    """Record one PTY master without sharing descriptor ownership with its caller."""

    def __init__(
        self,
        reader_fd: int,
        wake_read_fd: int,
        wake_write_fd: int,
        stream: BinaryIO,
        recording_path: Path,
        *,
        max_tail_bytes: int,
        timing_stream: BinaryIO | None = None,
    ) -> None:
        self._timing_stream: BinaryIO | None = timing_stream
        self.timing_path: Path | None = None
        self._recorded_bytes = 0
        self._reader_fd = reader_fd
        self._wake_read_fd = wake_read_fd
        self._wake_write_fd = wake_write_fd
        self._stream: BinaryIO | None = stream
        self.recording_path = recording_path
        self.max_tail_bytes = max(0, max_tail_bytes)
        self._stop = threading.Event()
        self._finalized = threading.Event()
        self._reader_done = threading.Event()
        self._wake_writer_closed = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._wake_lock = threading.Lock()
        self._recording_lock = threading.Lock()
        self._tail_lock = threading.Lock()
        self._stop_deadline: float | None = None
        self._pending_output = bytearray()
        self._pending_output_dropped_bytes = 0
        self._reader_error: OSError | None = None
        self.reader_eof_reached = False
        self.reader_join_timed_out = False
        self.reader_eof_join_timed_out = False
        self.recording_lock_timed_out = False
        self.reader_state_lock_timed_out = False
        self.wake_timed_out = False
        self.recording_incomplete = False
        self._thread: threading.Thread | None = None
        self._resources_released = False

    @classmethod
    def start(
        cls,
        master_fd: int,
        recording_path: Path,
        *,
        mode: str = "ab",
        max_tail_bytes: int = _TAIL_BYTES,
        thread_name: str = "owned-pty-reader",
        timing_path: Path | None = None,
    ) -> OwnedPtyReader:
        """Duplicate the master and open the output before starting the reader.

        With ``timing_path`` the reader also writes a sidecar: a header line naming the
        clock (``time.monotonic()``) and one line per chunk read with its return time,
        byte offset in the recording and length. The recording itself is unchanged.
        """

        recording_path.parent.mkdir(parents=True, exist_ok=True)
        recording_existed = recording_path.exists()
        reader_fd = wake_read_fd = wake_write_fd = -1
        stream: BinaryIO | None = None
        timing_stream: BinaryIO | None = None
        reader: OwnedPtyReader | None = None
        try:
            reader_fd = os.dup(master_fd)
            os.set_blocking(reader_fd, False)
            wake_read_fd, wake_write_fd = os.pipe()
            os.set_blocking(wake_read_fd, False)
            os.set_blocking(wake_write_fd, False)
            stream = recording_path.open(mode, buffering=0)
            start_offset = 0
            if timing_path is not None:
                start_offset = recording_path.stat().st_size if "a" in mode else 0
                timing_path.parent.mkdir(parents=True, exist_ok=True)
                timing_stream = timing_path.open("wb", buffering=0)
                header = {"clock": "monotonic", "recording": recording_path.name, "start_offset": start_offset}
                timing_stream.write(json.dumps(header).encode("utf-8") + b"\n")
            reader = cls(
                reader_fd,
                wake_read_fd,
                wake_write_fd,
                stream,
                recording_path,
                max_tail_bytes=max_tail_bytes,
                timing_stream=timing_stream,
            )
            reader.timing_path = timing_path
            reader._recorded_bytes = start_offset
            reader._thread = threading.Thread(target=reader._run, daemon=True, name=thread_name)
            reader._thread.start()
            return reader
        except BaseException:
            if reader is not None:
                reader._abort_start()
            else:
                for opened in (stream, timing_stream):
                    if opened is not None:
                        try:
                            opened.close()
                        except OSError:
                            pass
                if timing_path is not None:
                    try:
                        timing_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                for fd in (reader_fd, wake_read_fd, wake_write_fd):
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                if not recording_existed:
                    try:
                        recording_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise

    @property
    def closed(self) -> bool:
        return self._reader_done.is_set() and self._wake_writer_closed.is_set()

    @property
    def wake_writer_closed(self) -> bool:
        return self._wake_writer_closed.is_set()

    @property
    def dropped_output_bytes(self) -> int:
        return self._pending_output_dropped_bytes

    @property
    def reader_error(self) -> str | None:
        error = self._reader_error
        return None if error is None else f"{type(error).__name__}: {error}"

    def _mark_incomplete(self) -> None:
        self.recording_incomplete = True
        self._stop.set()
        self._finalized.set()
        for doomed in (self.recording_path, self.timing_path):
            if doomed is None:
                continue
            try:
                doomed.unlink(missing_ok=True)
            except OSError:
                pass

    def mark_incomplete(self) -> None:
        self._mark_incomplete()
        self._signal_reader()
        self._close_wake_writer()

    def _signal_reader(self) -> bool:
        if not self._wake_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            self.wake_timed_out = True
            return False
        try:
            wake_fd = self._wake_write_fd
            if wake_fd < 0 or self._reader_done.is_set():
                return True
            try:
                os.write(wake_fd, b"\x00")
            except BlockingIOError:
                pass
            except OSError as exc:
                if exc.errno not in {errno.EPIPE, errno.EBADF} and not self._reader_done.is_set():
                    self.wake_timed_out = True
                    return False
            return True
        finally:
            self._wake_lock.release()

    def _close_wake_writer(self) -> bool:
        if not self._wake_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            self.wake_timed_out = True
            self._mark_incomplete()
            return False
        try:
            wake_fd = self._wake_write_fd
            self._wake_write_fd = -1
        finally:
            self._wake_lock.release()
        if wake_fd >= 0:
            try:
                os.close(wake_fd)
            except OSError:
                pass
        self._wake_writer_closed.set()
        return True

    def _append_tail(self, chunk: bytes) -> bool:
        if not self._tail_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            self.reader_state_lock_timed_out = True
            self._mark_incomplete()
            return False
        try:
            overflow = len(self._pending_output) + len(chunk) - self.max_tail_bytes
            if overflow > 0:
                dropped = min(overflow, len(self._pending_output))
                if dropped:
                    del self._pending_output[:dropped]
                chunk = chunk[overflow - dropped :]
                self._pending_output_dropped_bytes += overflow
            self._pending_output.extend(chunk)
            return True
        finally:
            self._tail_lock.release()

    def _run(self) -> None:
        try:
            while not self._finalized.is_set():
                stop_deadline = self._stop_deadline if self._stop.is_set() else None
                now = time.monotonic()
                if stop_deadline is not None and now >= stop_deadline:
                    return
                timeout = _POLL_SECONDS if stop_deadline is None else min(_POLL_SECONDS, max(0.0, stop_deadline - now))
                reader_fd = self._reader_fd
                wake_fd = self._wake_read_fd
                if reader_fd < 0 or wake_fd < 0:
                    return
                try:
                    ready, _, _ = select.select([reader_fd, wake_fd], [], [], timeout)
                except InterruptedError:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        self.reader_eof_reached = True
                    else:
                        self._reader_error = exc
                    return
                if wake_fd in ready:
                    while True:
                        try:
                            wake = os.read(wake_fd, 4096)
                        except BlockingIOError:
                            break
                        except OSError as exc:
                            self._reader_error = exc
                            return
                        if not wake:
                            return
                    stop_deadline = self._stop_deadline if self._stop.is_set() else None
                    if stop_deadline is not None and time.monotonic() >= stop_deadline:
                        return
                if reader_fd not in ready:
                    if stop_deadline is not None and time.monotonic() >= stop_deadline:
                        return
                    continue
                try:
                    chunk = os.read(reader_fd, 64 * 1024)
                    read_at = time.monotonic()
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        self.reader_eof_reached = True
                    elif exc.errno != errno.EBADF:
                        self._reader_error = exc
                    return
                if not chunk:
                    self.reader_eof_reached = True
                    return
                if not self._recording_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
                    self.recording_lock_timed_out = True
                    self._mark_incomplete()
                    return
                try:
                    if self._finalized.is_set():
                        return
                    try:
                        stream = self._stream
                        if stream is not None:
                            view = memoryview(chunk)
                            offset = 0
                            while offset < len(view):
                                written = stream.write(view[offset:])
                                if written is None or written <= 0:
                                    raise OSError(errno.EIO, "PTY recording write made no progress")
                                offset += written
                        self._log_chunk_time(read_at, len(chunk))
                    except OSError as exc:
                        self._reader_error = exc
                        self._mark_incomplete()
                        return
                finally:
                    self._recording_lock.release()
                if self._finalized.is_set() or not self._append_tail(chunk):
                    return
        finally:
            self._release_reader_resources()

    def _log_chunk_time(self, read_at: float, size: int) -> None:
        timing = self._timing_stream
        if timing is not None:
            entry = {"t": read_at, "o": self._recorded_bytes, "n": size}
            timing.write(json.dumps(entry, separators=(",", ":")).encode("utf-8") + b"\n")
        self._recorded_bytes += size

    def _release_reader_resources(self) -> None:
        if self._resources_released:
            return
        self._resources_released = True
        stream = self._stream
        self._stream = None
        timing = self._timing_stream
        self._timing_stream = None
        if timing is not None:
            try:
                timing.close()
            except OSError as exc:
                self._reader_error = exc
                self._mark_incomplete()
        if stream is not None:
            try:
                stream.close()
            except OSError as exc:
                self._reader_error = exc
                self._mark_incomplete()
        self._close_wake_writer()
        for name in ("_reader_fd", "_wake_read_fd"):
            fd = getattr(self, name)
            setattr(self, name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as exc:
                    self._reader_error = exc
                    self._mark_incomplete()
        self._reader_done.set()

    def _abort_start(self) -> None:
        self._stop_deadline = time.monotonic()
        self._mark_incomplete()
        self._signal_reader()
        self._close_wake_writer()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_JOIN_RETRY_SECONDS)
        if thread is None or not thread.is_alive():
            if not self._reader_done.is_set():
                self._release_reader_resources()

    def drain(self) -> bytes:
        if not self._tail_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            self.reader_state_lock_timed_out = True
            self._mark_incomplete()
            return b""
        try:
            if self._reader_error is not None:
                raise self._reader_error
            output = bytes(self._pending_output)
            self._pending_output.clear()
            return output
        finally:
            self._tail_lock.release()

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        if thread.is_alive():
            self.reader_join_timed_out = True
            return False
        return True

    def _seal_recording(self) -> bool:
        if not self._recording_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            self.recording_lock_timed_out = True
            self._mark_incomplete()
            return False
        try:
            self._finalized.set()
        finally:
            self._recording_lock.release()
        return not self.recording_incomplete

    def finalize_recording(self, timeout: float = 2.0) -> bool:
        if not self._lifecycle_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            self._mark_incomplete()
            self._signal_reader()
            self._close_wake_writer()
            self.join(_JOIN_RETRY_SECONDS)
            return False
        try:
            if self._finalized.is_set():
                self._signal_reader()
                self._close_wake_writer()
                self.join(_JOIN_RETRY_SECONDS)
                return self.reader_eof_reached and self._seal_recording()
            timeout = max(0.0, timeout)
            self._stop_deadline = time.monotonic() + timeout
            self._stop.set()
            self._signal_reader()
            thread = self._thread
            if thread is not None:
                thread.join(timeout=timeout + 1.0)
                if thread.is_alive():
                    self.reader_join_timed_out = True
                    self._mark_incomplete()
                    self._signal_reader()
                    self._close_wake_writer()
                    thread.join(timeout=_JOIN_RETRY_SECONDS)
                    if thread.is_alive():
                        self._mark_incomplete()
                else:
                    self._close_wake_writer()
            if not self.reader_eof_reached:
                self._mark_incomplete()
            self._seal_recording()
            return self.reader_eof_reached and not self.recording_incomplete
        finally:
            self._lifecycle_lock.release()

    def close(self, timeout: float = 2.0) -> bool:
        """Finalize idempotently; the reader closes its own resources on exit."""

        return self.finalize_recording(timeout)

    def __enter__(self) -> OwnedPtyReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
