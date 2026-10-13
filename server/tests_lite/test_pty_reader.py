from __future__ import annotations

import os
import pty
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from zerg.qa import pty_reader
from zerg.qa.pty_reader import OwnedPtyReader


def test_reader_keeps_a_bounded_tail_and_counts_dropped_bytes(tmp_path: Path) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    reader = OwnedPtyReader.start(master_fd, recording, mode="ab", max_tail_bytes=64 * 1024)
    payload = b"a" * (128 * 1024) + b"z" * (128 * 1024)
    write_errors: list[BaseException] = []

    def write_output() -> None:
        try:
            for chunk in (payload[: 128 * 1024], payload[128 * 1024 :]):
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(slave_fd, remaining)
                    remaining = remaining[written:]
        except BaseException as exc:  # noqa: BLE001 - preserve the fixture failure for the assertion
            write_errors.append(exc)

    writer = threading.Thread(target=write_output)
    try:
        writer.start()
        writer.join(timeout=3.0)
        assert not writer.is_alive()
        assert write_errors == []

        expected_dropped = len(payload) - 64 * 1024
        deadline = time.monotonic() + 3.0
        while reader.dropped_output_bytes < expected_dropped and time.monotonic() < deadline:
            time.sleep(0.01)
        retained = reader.drain()
        assert len(retained) == 64 * 1024
        assert retained == b"z" * (64 * 1024)
        assert reader.dropped_output_bytes == len(payload) - len(retained)
        assert recording.stat().st_size == len(payload)
    finally:
        os.close(slave_fd)
        reader.close(timeout=0.5)
        os.close(master_fd)


def test_finalized_recording_refuses_late_output(tmp_path: Path) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    secret = b"provider-secret-marker"
    late_output = b"late-provider-secret"
    reader = OwnedPtyReader.start(master_fd, recording, mode="ab")
    try:
        os.write(slave_fd, secret)
        deadline = time.monotonic() + 2.0
        while (not recording.exists() or secret not in recording.read_bytes()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert secret in recording.read_bytes()

        assert reader.finalize_recording(timeout=0.05) is False
        assert reader.recording_incomplete is True
        assert not recording.exists()
        os.write(slave_fd, late_output)
        time.sleep(0.05)

        assert not recording.exists()
        assert reader.closed is True
    finally:
        os.close(slave_fd)
        reader.close(timeout=0.0)
        os.close(master_fd)


def test_reader_start_failure_rolls_back_all_reader_resources(tmp_path: Path, monkeypatch) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    opened_streams = []
    failed_readers: list[OwnedPtyReader] = []
    original_open = Path.open
    allocated_fds: list[int] = []
    original_dup = pty_reader.os.dup
    original_pipe = pty_reader.os.pipe

    def track_dup(fd: int) -> int:
        duplicate = original_dup(fd)
        allocated_fds.append(duplicate)
        return duplicate

    def track_pipe() -> tuple[int, int]:
        read_fd, write_fd = original_pipe()
        allocated_fds.extend((read_fd, write_fd))
        return read_fd, write_fd

    def track_open(path: Path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == recording:
            opened_streams.append(stream)
        return stream

    def fail_start(thread: threading.Thread) -> None:
        failed_readers.append(thread._target.__self__)
        raise RuntimeError("injected reader thread start failure")

    monkeypatch.setattr(Path, "open", track_open)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    monkeypatch.setattr(pty_reader.os, "dup", track_dup)
    monkeypatch.setattr(pty_reader.os, "pipe", track_pipe)
    try:
        with pytest.raises(RuntimeError, match="injected reader thread start failure"):
            OwnedPtyReader.start(master_fd, recording, mode="wb")

        assert len(failed_readers) == 1
        reader = failed_readers[0]
        assert reader.closed is True
        assert reader.recording_incomplete is True
        assert reader._reader_fd == -1
        assert reader._wake_read_fd == -1
        assert reader._wake_write_fd == -1
        assert len(opened_streams) == 1 and opened_streams[0].closed
        for fd in allocated_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert not recording.exists()
        os.fstat(master_fd)
    finally:
        os.close(slave_fd)
        os.close(master_fd)


def test_recording_open_failure_closes_duplicated_descriptors(tmp_path: Path, monkeypatch) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    allocated_fds: list[int] = []
    original_dup = pty_reader.os.dup
    original_pipe = pty_reader.os.pipe
    original_open = Path.open

    def track_dup(fd: int) -> int:
        duplicate = original_dup(fd)
        allocated_fds.append(duplicate)
        return duplicate

    def track_pipe() -> tuple[int, int]:
        read_fd, write_fd = original_pipe()
        allocated_fds.extend((read_fd, write_fd))
        return read_fd, write_fd

    def fail_recording_open(path: Path, *args, **kwargs):
        if path == recording:
            raise OSError("injected recording open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(pty_reader.os, "dup", track_dup)
    monkeypatch.setattr(pty_reader.os, "pipe", track_pipe)
    monkeypatch.setattr(Path, "open", fail_recording_open)
    try:
        with pytest.raises(OSError, match="injected recording open failure"):
            OwnedPtyReader.start(master_fd, recording)

        assert len(allocated_fds) == 3
        for fd in allocated_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
        os.fstat(master_fd)
        assert not recording.exists()
    finally:
        os.close(slave_fd)
        os.close(master_fd)


def test_recording_lock_timeout_closes_stream_after_the_writer_returns(tmp_path: Path, monkeypatch) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    writer_started = threading.Event()
    release_writer = threading.Event()

    class StalledStream:
        closed = False

        def write(self, chunk: bytes) -> int:
            writer_started.set()
            release_writer.wait(timeout=5.0)
            return len(chunk)

        def close(self) -> None:
            self.closed = True

    stream = StalledStream()
    original_open = Path.open

    def fake_open(path: Path, *args, **kwargs):
        return stream if path == recording else original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    reader = OwnedPtyReader.start(master_fd, recording, mode="ab")
    try:
        os.write(slave_fd, b"stalled output")
        assert writer_started.wait(timeout=2.0)
        started = time.monotonic()
        assert reader.close(timeout=0.0) is False
        assert time.monotonic() - started < 2.5
        assert reader.recording_lock_timed_out is True
        assert reader.recording_incomplete is True
        assert reader.closed is False
        assert stream.closed is False
        assert not recording.exists()

        release_writer.set()
        assert reader.join(timeout=2.0) is True
        assert stream.closed is True
        assert reader.closed is True
    finally:
        release_writer.set()
        os.close(slave_fd)
        reader.close(timeout=0.0)
        os.close(master_fd)


def test_delayed_reader_cannot_recreate_recording_after_close(tmp_path: Path, monkeypatch) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    reader_waiting = threading.Event()
    release_reader = threading.Event()
    original_run = threading.Thread.run

    def gated_run(thread: threading.Thread) -> None:
        if thread.name == "gated-owned-pty-reader":
            reader_waiting.set()
            release_reader.wait()
        original_run(thread)

    monkeypatch.setattr(threading.Thread, "run", gated_run)
    reader = OwnedPtyReader.start(master_fd, recording, thread_name="gated-owned-pty-reader")
    try:
        assert reader_waiting.wait(timeout=2.0)
        assert recording.exists()
        assert reader.close(timeout=0.0) is False
        assert reader.recording_incomplete is True
        assert not recording.exists()

        release_reader.set()
        assert reader.join(timeout=2.0) is True
        assert reader.closed is True
        assert not recording.exists()
    finally:
        release_reader.set()
        reader.close(timeout=0.0)
        os.close(slave_fd)
        os.close(master_fd)


def test_fifty_readers_close_without_leaking_descriptors(tmp_path: Path) -> None:
    script = (
        "import gc, os, pty, sys\n"
        "from pathlib import Path\n"
        "from zerg.qa.pty_reader import OwnedPtyReader\n"
        "root = Path(sys.argv[1])\n"
        "fd_count = lambda: len(os.listdir('/dev/fd'))\n"
        "baseline = fd_count()\n"
        "for index in range(50):\n"
        "    master_fd, slave_fd = pty.openpty()\n"
        "    reader = OwnedPtyReader.start(master_fd, root / f'terminal-{index}.raw')\n"
        "    try:\n"
        "        reader.close(timeout=0.0)\n"
        "        assert reader.wake_writer_closed and reader.closed\n"
        "    finally:\n"
        "        os.close(slave_fd)\n"
        "        os.close(master_fd)\n"
        "gc.collect()\n"
        "assert fd_count() == baseline, (baseline, fd_count())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_close_is_idempotent_and_releases_every_owned_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_fd, slave_fd = pty.openpty()
    allocated_fds: list[int] = []
    original_dup = pty_reader.os.dup
    original_pipe = pty_reader.os.pipe

    def track_dup(fd: int) -> int:
        duplicate = original_dup(fd)
        allocated_fds.append(duplicate)
        return duplicate

    def track_pipe() -> tuple[int, int]:
        read_fd, write_fd = original_pipe()
        allocated_fds.extend((read_fd, write_fd))
        return read_fd, write_fd

    monkeypatch.setattr(pty_reader.os, "dup", track_dup)
    monkeypatch.setattr(pty_reader.os, "pipe", track_pipe)
    reader = OwnedPtyReader.start(master_fd, tmp_path / "terminal.raw")
    try:
        assert reader.close(timeout=0.1) is False
        assert reader.closed is True
        assert reader.recording_incomplete is True
        assert reader.close(timeout=0.1) is False
        assert reader.closed is True
        assert len(allocated_fds) == 3
        for fd in allocated_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
        os.fstat(master_fd)
        os.fstat(slave_fd)
    finally:
        reader.close(timeout=0.0)
        os.close(slave_fd)
        os.close(master_fd)


def test_natural_eof_closes_both_wake_pipe_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_fd, slave_fd = pty.openpty()
    wake_fds: list[int] = []
    original_pipe = pty_reader.os.pipe

    def track_pipe() -> tuple[int, int]:
        read_fd, write_fd = original_pipe()
        wake_fds.extend((read_fd, write_fd))
        return read_fd, write_fd

    monkeypatch.setattr(pty_reader.os, "pipe", track_pipe)
    reader = OwnedPtyReader.start(master_fd, tmp_path / "terminal.raw")
    try:
        os.close(slave_fd)
        slave_fd = -1
        deadline = time.monotonic() + 2.0
        while not reader.closed and time.monotonic() < deadline:
            time.sleep(0.01)

        assert reader.closed is True
        assert reader.reader_eof_reached is True
        assert reader.recording_incomplete is False
        assert len(wake_fds) == 2
        for fd in wake_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        reader.close(timeout=0.0)
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


def test_reader_join_is_bounded_when_a_child_keeps_the_slave_open(tmp_path: Path) -> None:
    master_fd, slave_fd = pty.openpty()
    holder: subprocess.Popen[bytes] | None = None
    reader: OwnedPtyReader | None = None
    try:
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            pass_fds=(slave_fd,),
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        reader = OwnedPtyReader.start(master_fd, tmp_path / "terminal.raw")

        join_started = time.monotonic()
        assert reader.join(timeout=0.01) is False
        assert time.monotonic() - join_started < 0.5
        assert reader.reader_join_timed_out is True

        close_started = time.monotonic()
        assert reader.close(timeout=0.05) is False
        assert time.monotonic() - close_started < 1.5
        assert reader.closed is True
        assert reader.recording_incomplete is True
    finally:
        if reader is not None:
            reader.close(timeout=0.0)
        if holder is not None:
            holder.terminate()
            holder.wait(timeout=3.0)
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


def test_reader_holds_no_lock_during_select_or_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_fd, slave_fd = pty.openpty()
    select_entered = threading.Event()
    release_select = threading.Event()
    locks: list[Any] = []
    observed: list[str] = []
    violations: list[str] = []
    original_select = pty_reader.select.select
    original_read = pty_reader.os.read

    class ObservedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.owner: int | None = None

        def acquire(self, timeout: float | None = None) -> bool:
            acquired = self._lock.acquire() if timeout is None else self._lock.acquire(timeout=timeout)
            if acquired:
                self.owner = threading.get_ident()
            return acquired

        def release(self) -> None:
            self.owner = None
            self._lock.release()

    def assert_unlocked(operation: str) -> None:
        if any(lock.owner == threading.get_ident() for lock in locks):
            violations.append(operation)
        observed.append(operation)

    def checked_select(readers, writers, exceptional, timeout):
        if not select_entered.is_set():
            select_entered.set()
            if not release_select.wait(timeout=2.0):
                violations.append("select gate timed out")
        assert_unlocked("select")
        return original_select(readers, writers, exceptional, timeout)

    def checked_read(fd: int, size: int) -> bytes:
        assert_unlocked("read")
        return original_read(fd, size)

    monkeypatch.setattr(pty_reader.select, "select", checked_select)
    monkeypatch.setattr(pty_reader.os, "read", checked_read)
    reader = OwnedPtyReader.start(master_fd, tmp_path / "terminal.raw")
    try:
        assert select_entered.wait(timeout=2.0)
        locks.extend(ObservedLock() for _ in range(4))
        reader._lifecycle_lock, reader._wake_lock, reader._recording_lock, reader._tail_lock = locks
        payload = b"output read without holding a lock"
        os.write(slave_fd, payload)
        release_select.set()

        deadline = time.monotonic() + 2.0
        while payload not in reader.recording_path.read_bytes() and time.monotonic() < deadline:
            time.sleep(0.01)

        assert payload in reader.recording_path.read_bytes()
        assert "select" in observed
        assert "read" in observed
        assert violations == []
    finally:
        release_select.set()
        reader.close(timeout=0.5)
        os.close(slave_fd)
        os.close(master_fd)


def test_deadline_with_queued_output_marks_recording_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    recording = tmp_path / "terminal.raw"
    select_entered = threading.Event()
    release_select = threading.Event()
    wake_signaled = threading.Event()
    original_select = pty_reader.select.select
    original_signal = OwnedPtyReader._signal_reader

    def gate_select(readers, writers, exceptional, timeout):
        if threading.current_thread().name == "deadline-owned-reader" and not select_entered.is_set():
            select_entered.set()
            if not release_select.wait(timeout=2.0):
                return [], [], []
            return [readers[1]], [], []
        return original_select(readers, writers, exceptional, timeout)

    def signal_reader(reader: OwnedPtyReader) -> bool:
        result = original_signal(reader)
        wake_signaled.set()
        return result

    monkeypatch.setattr(pty_reader.select, "select", gate_select)
    monkeypatch.setattr(OwnedPtyReader, "_signal_reader", signal_reader)
    reader = OwnedPtyReader.start(master_fd, recording, thread_name="deadline-owned-reader")
    payload = b"still queued when the drain deadline expires"
    finalize_result: list[bool] = []
    finalize_errors: list[BaseException] = []

    def finalize() -> None:
        try:
            finalize_result.append(reader.finalize_recording(timeout=0.01))
        except BaseException as exc:  # noqa: BLE001 - preserve finalizer-thread failures
            finalize_errors.append(exc)

    finalizer = threading.Thread(target=finalize)
    try:
        assert select_entered.wait(timeout=2.0)
        os.write(slave_fd, payload)
        finalizer.start()
        assert wake_signaled.wait(timeout=1.0)
        time.sleep(0.05)
        release_select.set()
        finalizer.join(timeout=2.0)
        assert not finalizer.is_alive()
        assert finalize_errors == []
        assert finalize_result == [False]
        assert reader.reader_eof_reached is False
        assert reader.reader_join_timed_out is False
        assert reader.recording_incomplete is True
        assert reader.closed is True
        assert not recording.exists()
        assert os.read(master_fd, len(payload)) == payload
    finally:
        release_select.set()
        if finalizer.is_alive():
            finalizer.join(timeout=2.0)
        reader.close(timeout=0.0)
        os.close(slave_fd)
        os.close(master_fd)


def test_teardown_preserves_reader_error_for_explicit_drain(tmp_path: Path) -> None:
    master_fd, slave_fd = pty.openpty()
    reader = OwnedPtyReader.start(master_fd, tmp_path / "terminal.raw")
    error = OSError("injected reader failure")
    reader._reader_error = error
    try:
        with pytest.raises(OSError) as caught:
            reader.drain()
        assert caught.value is error
        assert reader.reader_error == "OSError: injected reader failure"

        assert reader.close(timeout=0.0) is False
        assert reader.recording_incomplete is True
        assert reader.reader_error == "OSError: injected reader failure"
    finally:
        reader.close(timeout=0.0)
        os.close(slave_fd)
        os.close(master_fd)


def test_concurrent_finalize_after_pipe_teardown_keeps_complete_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    payload = b"complete PTY recording"
    reader = OwnedPtyReader.start(master_fd, recording)
    wake_read_fd = reader._wake_read_fd
    wake_read_closed = threading.Event()
    release_wake_close = threading.Event()
    wake_signaled = threading.Event()
    writer_values_at_read_close: list[int] = []
    finalize_result: list[bool] = []
    original_close = pty_reader.os.close
    original_signal = OwnedPtyReader._signal_reader

    def pause_after_wake_read_close(fd: int) -> None:
        original_close(fd)
        if fd == wake_read_fd:
            writer_values_at_read_close.append(reader._wake_write_fd)
            wake_read_closed.set()
            release_wake_close.wait(timeout=2.0)

    def signal_reader(owner: OwnedPtyReader) -> bool:
        result = original_signal(owner)
        wake_signaled.set()
        return result

    monkeypatch.setattr(pty_reader.os, "close", pause_after_wake_read_close)
    monkeypatch.setattr(OwnedPtyReader, "_signal_reader", signal_reader)
    try:
        os.write(slave_fd, payload)
        deadline = time.monotonic() + 2.0
        while (not recording.exists() or recording.read_bytes() != payload) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert recording.read_bytes() == payload
        os.close(slave_fd)
        slave_fd = -1
        assert wake_read_closed.wait(timeout=2.0)

        finalizer = threading.Thread(target=lambda: finalize_result.append(reader.finalize_recording(timeout=0.1)))
        finalizer.start()
        assert wake_signaled.wait(timeout=1.0)
        release_wake_close.set()
        finalizer.join(timeout=2.0)
        assert not finalizer.is_alive()

        assert writer_values_at_read_close == [-1]
        assert finalize_result == [True]
        assert reader.reader_eof_reached is True
        assert reader.recording_incomplete is False
        assert reader.closed is True
        assert recording.read_bytes() == payload
    finally:
        release_wake_close.set()
        if slave_fd >= 0:
            os.close(slave_fd)
        reader.close(timeout=0.0)
        os.close(master_fd)


def test_reader_retries_short_recording_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    wrapped_streams = []
    original_open = Path.open

    class ShortWriteStream:
        def __init__(self, stream) -> None:
            self.stream = stream
            self.write_calls = 0

        def write(self, data) -> int:
            self.write_calls += 1
            return self.stream.write(data[:7])

        def close(self) -> None:
            self.stream.close()

    def short_open(path: Path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == recording and args and args[0] == "ab":
            wrapped = ShortWriteStream(stream)
            wrapped_streams.append(wrapped)
            return wrapped
        return stream

    monkeypatch.setattr(Path, "open", short_open)
    reader = OwnedPtyReader.start(master_fd, recording)
    payload = b"short-write-loop-" * 12
    try:
        os.write(slave_fd, payload)
        os.close(slave_fd)
        slave_fd = -1
        deadline = time.monotonic() + 2.0
        while (not reader.closed or recording.read_bytes() != payload) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert len(wrapped_streams) == 1
        assert wrapped_streams[0].write_calls > 1
        assert recording.read_bytes() == payload
        assert reader.reader_eof_reached is True
        assert reader.recording_incomplete is False
        assert reader.reader_error is None
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        reader.close(timeout=0.0)
        os.close(master_fd)


@pytest.mark.parametrize("write_raises", [False, True])
def test_failed_recording_write_marks_incomplete_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_raises: bool,
) -> None:
    master_fd, slave_fd = pty.openpty()
    recording = tmp_path / "terminal.raw"
    write_attempted = threading.Event()
    original_open = Path.open

    class FailedWriteStream:
        def write(self, _data) -> int:
            write_attempted.set()
            if write_raises:
                raise OSError("injected recorder write failure")
            return 0

        def close(self) -> None:
            pass

    stream = FailedWriteStream()

    def failed_open(path: Path, *args, **kwargs):
        if path == recording and args and args[0] == "ab":
            return stream
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failed_open)
    reader = OwnedPtyReader.start(master_fd, recording)
    try:
        os.write(slave_fd, b"trigger failed recording write")
        assert write_attempted.wait(timeout=2.0)
        assert reader.join(timeout=2.0) is True
        assert reader.recording_incomplete is True
        if write_raises:
            assert reader.reader_error == "OSError: injected recorder write failure"
        else:
            assert reader.reader_error == "OSError: [Errno 5] PTY recording write made no progress"
        assert not recording.exists()
        assert reader.close(timeout=0.0) is False
    finally:
        reader.close(timeout=0.0)
        os.close(slave_fd)
        os.close(master_fd)
