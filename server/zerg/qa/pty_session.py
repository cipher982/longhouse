"""Small owned-PTY primitive shared by live provider qualification harnesses."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from zerg.qa.pty_reader import OwnedPtyReader
from zerg.qa.wait_kind import PROVIDER_PROCESS
from zerg.qa.wait_kind import expired

_PTY_CLOSE_LOCK_SECONDS = 1.0
_PTY_WRITE_TIMEOUT_SECONDS = 5.0


@dataclass
class ProviderPtySession:
    process: subprocess.Popen[bytes]
    master_fd: int
    terminal_path: Path
    _reader: OwnedPtyReader
    _stop_reader: threading.Event
    _write_lock: threading.Lock
    _master_lock: threading.Lock
    _master_ref: list[int]
    reader_join_timed_out: bool = False
    reader_wake_timed_out: bool = False
    recording_incomplete: bool = False
    reader_error: str | None = None
    master_close_timed_out: bool = False
    recording_lock_timed_out: bool = False
    text_settle_seconds: float = 0.3
    escape_settle_seconds: float = 0.1
    steer_queue_settle_seconds: float = 0.5

    @classmethod
    def start(
        cls,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        terminal_path: Path,
        rows: int = 40,
        columns: int = 132,
        thread_name: str = "provider-qualification-terminal-drain",
    ) -> ProviderPtySession:
        terminal_path.parent.mkdir(parents=True, exist_ok=True)
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

            def child_setup() -> None:
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=child_setup,
            )
        except BaseException:
            for fd in (master_fd, slave_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        stop_reader = threading.Event()
        master_lock = threading.Lock()
        master_ref = [master_fd]
        try:
            reader = OwnedPtyReader.start(master_fd, terminal_path, mode="ab", thread_name=thread_name)
        except BaseException:
            stop_reader.set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            master_ref[0] = -1
            try:
                os.close(master_fd)
            except OSError:
                pass
            raise
        return cls(
            process=process,
            master_fd=master_fd,
            terminal_path=terminal_path,
            _reader=reader,
            _stop_reader=stop_reader,
            _write_lock=threading.Lock(),
            _master_lock=master_lock,
            _master_ref=master_ref,
        )

    def alive(self) -> bool:
        return self.process.poll() is None

    def _mark_recording_incomplete(self) -> None:
        self.recording_incomplete = True
        self._reader.mark_incomplete()

    def _close_master(self) -> bool:
        if not self._master_lock.acquire(timeout=_PTY_CLOSE_LOCK_SECONDS):
            self.master_close_timed_out = True
            self._mark_recording_incomplete()
            return False
        try:
            master_fd = self._master_ref[0]
            self._master_ref[0] = -1
            self.master_fd = -1
        finally:
            self._master_lock.release()
        if master_fd >= 0:
            try:
                os.close(master_fd)
            except OSError:
                pass
        return True

    def _write_master(self, value: bytes) -> None:
        if not value:
            return
        deadline = time.monotonic() + _PTY_WRITE_TIMEOUT_SECONDS
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._master_lock.acquire(timeout=remaining):
            raise TimeoutError("provider PTY master remained locked")
        try:
            master_fd = self._master_ref[0]
            if master_fd < 0 or self._stop_reader.is_set():
                raise OSError(errno.EBADF, os.strerror(errno.EBADF))
            writer_fd = os.dup(master_fd)
        finally:
            self._master_lock.release()
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("provider PTY input remained backpressured")
            os.set_blocking(writer_fd, False)
            view = memoryview(value)
            offset = 0
            while offset < len(view):
                if self._stop_reader.is_set():
                    raise OSError(errno.EBADF, os.strerror(errno.EBADF))
                try:
                    written = os.write(writer_fd, view[offset:])
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("provider PTY input remained backpressured")
                    try:
                        _, writable, _ = select.select([], [writer_fd], [], remaining)
                    except InterruptedError:
                        continue
                    if not writable:
                        raise TimeoutError("provider PTY input remained backpressured")
                    continue
                if written <= 0:
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
                offset += written
        finally:
            os.close(writer_fd)

    def write(self, value: bytes) -> None:
        if not self.alive():
            raise RuntimeError(f"provider process exited before PTY write ({self.process.returncode})")
        with self._write_lock:
            self._write_master(value)

    def submit_idle(self, text: str) -> None:
        if not self.alive():
            raise RuntimeError(f"provider process exited before submit ({self.process.returncode})")
        with self._write_lock:
            self._write_master(text.encode("utf-8"))
            time.sleep(self.text_settle_seconds)
            self._write_master(b"\x1b")
            time.sleep(self.escape_settle_seconds)
            self._write_master(b"\r")

    def submit_line(self, text: str) -> None:
        if not self.alive():
            raise RuntimeError(f"provider process exited before submit ({self.process.returncode})")
        with self._write_lock:
            self._write_master(text.encode("utf-8"))
            time.sleep(self.text_settle_seconds)
            self._write_master(b"\r")

    def interrupt(self) -> None:
        self.write(b"\x03")

    def submit_active(self, text: str) -> None:
        """Inject text into the generation that is already running.

        Cursor's TUI needs two Enters: text plus Enter only *queues* a
        follow-up, and Enter on the now-empty prompt injects that queued
        message into the running generation at its next tool boundary. One
        Enter is exactly the `cursor_steer_queue_only` negative control
        (engine/src/cursor_helm_launcher.rs pops the second CR to simulate a
        broken steer), so a one-Enter harness reports a working provider as
        unsupported -- which is what factory run aa7770ad did against
        2026.09.10, a version with native steer. No ESC: it clears the queue.
        """

        if not self.alive():
            raise RuntimeError(f"provider process exited before submit ({self.process.returncode})")
        with self._write_lock:
            self._write_master(text.encode("utf-8"))
            time.sleep(self.text_settle_seconds)
            self._write_master(b"\r")
            time.sleep(self.steer_queue_settle_seconds)
            self._write_master(b"\r")

    @staticmethod
    def _group_alive(process_group: int) -> bool:
        """Whether any process still belongs to an owned process group."""

        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def close(self) -> None:
        """Stop the owned PTY reader and reap the whole provider process group."""

        self._stop_reader.set()
        process_group = self.process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            pass
        deadline = time.monotonic() + 5
        while (self.process.poll() is None or self._group_alive(process_group)) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._group_alive(process_group):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
        if self.alive():
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if not self._reader.close():
            self._mark_recording_incomplete()
        self.reader_join_timed_out = self._reader.reader_join_timed_out
        self.reader_wake_timed_out = self._reader.wake_timed_out
        self.recording_incomplete = self._reader.recording_incomplete
        self.reader_error = self._reader.reader_error
        self.recording_lock_timed_out = self._reader.recording_lock_timed_out
        self._close_master()


def wait_for_terminal_quiescence(
    session: ProviderPtySession,
    *,
    timeout: float,
    minimum_bytes: int = 1000,
    stable_seconds: float = 2.0,
) -> None:
    """Wait for an owned provider TUI to stop rendering startup/activity output."""

    last_size = -1
    unchanged_since = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not session.alive():
            raise RuntimeError(f"provider TUI exited before quiescence ({session.process.returncode})")
        try:
            size = session.terminal_path.stat().st_size
        except OSError:
            time.sleep(0.2)
            continue
        now = time.monotonic()
        if size != last_size:
            last_size = size
            unchanged_since = now
        elif size >= minimum_bytes and now - unchanged_since >= stable_seconds:
            return
        time.sleep(0.2)
    raise expired(RuntimeError("provider TUI did not reach terminal quiescence"), PROVIDER_PROCESS)
