"""close() must reap descendants that outlive the direct child.

start() runs the child through os.setsid(), so the provider leads its own
process group. A provider can leave a descendant in that group after the
direct child exits -- the Cursor Agent leaves a ``node ... worker-server``,
which a qualification run left alive after its artifact root was removed.
Keying the group signal off the direct child's liveness orphaned it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zerg.qa.pty_session import ProviderPtySession


def _group_members(process_group: int) -> list[int]:
    listing = subprocess.run(["ps", "-Ao", "pid=,pgid="], capture_output=True, text=True).stdout
    members = []
    for line in listing.splitlines():
        pid, _, pgid = line.strip().partition(" ")
        if pid.isdigit() and pgid.strip() == str(process_group):
            members.append(int(pid))
    return members


def test_close_reaps_descendants_after_the_direct_child_exits(tmp_path: Path) -> None:
    session = ProviderPtySession.start(
        # The session leader exiting hangs up the PTY, which SIGHUPs the foreground
        # group; ignoring HUP (inherited across exec) is what lets the
        # descendant genuinely outlive the direct child.
        argv=["/bin/sh", "-c", "trap '' HUP; sleep 300 & exit 0"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        terminal_path=tmp_path / "terminal.raw",
    )
    process_group = session.process.pid
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if session.process.poll() is not None and _group_members(process_group):
                break
            time.sleep(0.1)

        assert session.process.poll() is not None, "fixture's direct child did not exit"
        assert _group_members(process_group), "fixture did not leave a descendant to reap"

        session.close()

        deadline = time.monotonic() + 10
        while _group_members(process_group) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _group_members(process_group), "close() left descendants alive in the owned group"
    finally:
        for pid in _group_members(process_group):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        session.close()


def test_close_bounds_a_stuck_reader_and_drops_incomplete_recording(tmp_path: Path, monkeypatch) -> None:
    recording = tmp_path / "terminal.raw"
    reader_waiting = threading.Event()
    release_reader = threading.Event()
    original_run = threading.Thread.run

    def gated_run(thread: threading.Thread) -> None:
        if thread.name == "provider-qualification-terminal-drain":
            reader_waiting.set()
            release_reader.wait()
        original_run(thread)

    monkeypatch.setattr(threading.Thread, "run", gated_run)
    session = ProviderPtySession.start(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        terminal_path=recording,
    )
    reader = session._reader
    try:
        assert reader_waiting.wait(timeout=2.0)
        started = time.monotonic()
        session.close()

        assert time.monotonic() - started < 5.0
        assert reader.reader_join_timed_out is True
        assert session.recording_incomplete is True
        assert session.master_fd == -1
        assert not recording.exists()

        release_reader.set()
        assert reader.join(timeout=2.0) is True
        assert reader.closed is True
    finally:
        release_reader.set()
        session.close()


def test_backpressured_write_does_not_hold_master_lock(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import pty_session

    session = ProviderPtySession.start(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        terminal_path=tmp_path / "terminal.raw",
    )
    monkeypatch.setattr(pty_session, "_PTY_WRITE_TIMEOUT_SECONDS", 0.2)
    writer_waiting = threading.Event()
    release_select = threading.Event()
    original_select = pty_session.select.select
    original_write = pty_session.os.write
    owned_master = session.master_fd

    def backpressured_write(fd, value):
        if fd != owned_master:
            raise BlockingIOError()
        return original_write(fd, value)

    def wait_until_released(readers, writers, exceptions, timeout=None):
        if writers:
            writer_waiting.set()
            release_select.wait(timeout=1.0)
            return [], [], []
        return original_select(readers, writers, exceptions, timeout)

    monkeypatch.setattr(pty_session.os, "write", backpressured_write)
    monkeypatch.setattr(pty_session.select, "select", wait_until_released)
    errors: list[BaseException] = []

    def write_input() -> None:
        try:
            session._write_master(b"x")
        except BaseException as exc:  # noqa: BLE001 - assert the bounded backpressure outcome
            errors.append(exc)

    writer = threading.Thread(target=write_input)
    try:
        writer.start()
        assert writer_waiting.wait(timeout=2.0), "fake PTY did not report input backpressure"
        assert session._master_lock.acquire(timeout=0.2), "blocked input write retained the master ownership lock"
        session._master_lock.release()

        started = time.monotonic()
        assert session._close_master() is True
        assert time.monotonic() - started < 1.0
        release_select.set()
        writer.join(timeout=1.0)
        assert not writer.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
    finally:
        release_select.set()
        session._stop_reader.set()
        session.close()
        writer.join(timeout=1.0)


def test_close_before_reader_runs_cannot_recreate_terminal_recording(tmp_path: Path, monkeypatch) -> None:
    reader_waiting = threading.Event()
    release_reader = threading.Event()
    original_run = threading.Thread.run

    def gated_run(thread: threading.Thread) -> None:
        if thread.name == "delayed-provider-recorder":
            reader_waiting.set()
            release_reader.wait()
        original_run(thread)

    monkeypatch.setattr(threading.Thread, "run", gated_run)
    recording = tmp_path / "terminal.raw"
    session = ProviderPtySession.start(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        terminal_path=recording,
        thread_name="delayed-provider-recorder",
    )
    try:
        assert reader_waiting.wait(timeout=2.0)
        assert recording.exists()
        started = time.monotonic()
        session.close()

        assert time.monotonic() - started < 5.0
        assert session.recording_incomplete is True
        assert not recording.exists()
        release_reader.set()
        assert session._reader.join(timeout=2.0) is True
        assert session._reader.closed is True
        assert not recording.exists()
    finally:
        release_reader.set()
        if not session._stop_reader.is_set():
            session.close()
        session._reader.join(timeout=2.0)


def test_input_write_gets_master_lock_while_reader_waits_in_select(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import pty_session

    reader_waiting = threading.Event()
    release_reader = threading.Event()
    original_select = pty_session.select.select

    def gate_reader_select(readers, writers, exceptions, timeout=None):
        if readers and not writers:
            reader_waiting.set()
            release_reader.wait(timeout=1.0)
        return original_select(readers, writers, exceptions, timeout)

    monkeypatch.setattr(pty_session, "_PTY_WRITE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pty_session.select, "select", gate_reader_select)
    session = ProviderPtySession.start(
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        terminal_path=tmp_path / "terminal.raw",
    )
    try:
        assert reader_waiting.wait(timeout=2.0), "reader did not enter select"
        started = time.monotonic()
        session._write_master(b"input")
        assert time.monotonic() - started < 0.05
    finally:
        release_reader.set()
        session.close()
