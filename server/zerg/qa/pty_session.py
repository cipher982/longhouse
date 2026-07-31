"""Small owned-PTY primitive shared by live provider qualification harnesses."""

from __future__ import annotations

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


@dataclass
class ProviderPtySession:
    process: subprocess.Popen[bytes]
    master_fd: int
    terminal_path: Path
    _reader: threading.Thread
    _stop_reader: threading.Event
    _write_lock: threading.Lock
    text_settle_seconds: float = 0.3
    escape_settle_seconds: float = 0.1

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
        master_fd, slave_fd = pty.openpty()
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
        os.close(slave_fd)
        stop_reader = threading.Event()
        terminal_path.parent.mkdir(parents=True, exist_ok=True)

        def drain() -> None:
            with terminal_path.open("ab", buffering=0) as output:
                while not stop_reader.is_set():
                    try:
                        ready, _, _ = select.select([master_fd], [], [], 0.2)
                    except (OSError, ValueError):
                        return
                    if not ready:
                        if process.poll() is not None:
                            return
                        continue
                    try:
                        chunk = os.read(master_fd, 65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    output.write(chunk)

        reader = threading.Thread(target=drain, daemon=True, name=thread_name)
        reader.start()
        return cls(
            process=process,
            master_fd=master_fd,
            terminal_path=terminal_path,
            _reader=reader,
            _stop_reader=stop_reader,
            _write_lock=threading.Lock(),
        )

    def alive(self) -> bool:
        return self.process.poll() is None

    def write(self, value: bytes) -> None:
        if not self.alive():
            raise RuntimeError(f"provider process exited before PTY write ({self.process.returncode})")
        with self._write_lock:
            os.write(self.master_fd, value)

    def submit_idle(self, text: str) -> None:
        if not self.alive():
            raise RuntimeError(f"provider process exited before submit ({self.process.returncode})")
        with self._write_lock:
            os.write(self.master_fd, text.encode("utf-8"))
            time.sleep(self.text_settle_seconds)
            os.write(self.master_fd, b"\x1b")
            time.sleep(self.escape_settle_seconds)
            os.write(self.master_fd, b"\r")

    def submit_line(self, text: str) -> None:
        if not self.alive():
            raise RuntimeError(f"provider process exited before submit ({self.process.returncode})")
        with self._write_lock:
            os.write(self.master_fd, text.encode("utf-8"))
            time.sleep(self.text_settle_seconds)
            os.write(self.master_fd, b"\r")

    def interrupt(self) -> None:
        self.write(b"\x03")

    def submit_active(self, text: str) -> None:
        self.submit_line(text)

    def close(self) -> None:
        self._stop_reader.set()
        if self.alive():
            process_group: int | None = None
            try:
                process_group = os.getpgid(self.process.pid)
                os.killpg(process_group, signal.SIGTERM)
            except (PermissionError, ProcessLookupError):
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if process_group is None:
                        process_group = os.getpgid(self.process.pid)
                    os.killpg(process_group, signal.SIGKILL)
                except (PermissionError, ProcessLookupError):
                    self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self._reader.join(timeout=2)


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
    raise RuntimeError("provider TUI did not reach terminal quiescence")
