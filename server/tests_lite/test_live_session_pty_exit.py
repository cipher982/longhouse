from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from zerg.qa import live_session_toolkit
from zerg.qa.live_session_toolkit import PtyProcess
from zerg.qa.provider_native_resume import SPECS


def test_wait_bounds_reader_join_when_descendant_holds_pty_slave(tmp_path: Path) -> None:
    binary = tmp_path / "fake-provider"
    marker = tmp_path / "child-pid"
    release = tmp_path / "release-child"
    recording = tmp_path / "terminal.tty"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    while not pathlib.Path(sys.argv[2]).exists():\n"
        "        time.sleep(0.01)\n"
        "    os._exit(0)\n"
        # Write then rename, so the test never reads a created but empty marker.
        "pathlib.Path(sys.argv[1] + '.tmp').write_text(str(child), encoding='utf-8')\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    process = PtyProcess(
        [str(binary), str(marker), str(release)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TERM": "xterm-256color"},
        recording=recording,
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 3.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "fake provider did not fork its PTY-holding child"
        child_pid = int(marker.read_text(encoding="utf-8"))
        assert process.process.wait(timeout=2.0) == 0

        started = time.monotonic()
        assert process.wait(timeout=0.2, reader_join_timeout=0.1) == 0
        assert time.monotonic() - started < 1.0
        assert process.reader_eof_reached is False
        assert process.reader_eof_join_timed_out is True
    finally:
        release.write_text("release", encoding="utf-8")
        eof_deadline = time.monotonic() + 2.0
        while not process.reader_eof_reached and time.monotonic() < eof_deadline:
            time.sleep(0.01)
        if not process.reader_eof_reached and child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            eof_deadline = time.monotonic() + 2.0
            while not process.reader_eof_reached and time.monotonic() < eof_deadline:
                time.sleep(0.01)
        process.close()

    assert process.reader_eof_reached is True


def test_finalized_recording_cannot_receive_late_output(tmp_path: Path) -> None:
    binary = tmp_path / "fake-provider"
    marker = tmp_path / "started"
    release = tmp_path / "release"
    recording = tmp_path / "terminal.tty"
    secret = b"provider-secret-marker"
    late_output = b"late-provider-secret"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        f"secret = {secret!r}\n"
        "os.write(1, secret)\n"
        "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        "    time.sleep(0.01)\n"
        f"try: os.write(1, {late_output!r})\n"
        "except OSError: pass\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    process = PtyProcess(
        [str(binary), str(marker), str(release)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TERM": "xterm-256color"},
        recording=recording,
    )
    try:
        deadline = time.monotonic() + 3.0
        while (not marker.exists() or not recording.exists() or secret not in recording.read_bytes()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        assert secret in recording.read_bytes()

        assert process.finalize_recording(timeout=0.5) is False
        assert process.reader_eof_join_timed_out is False
        assert process._reader.closed is True
        assert process.recording_incomplete is True
        assert not recording.exists()

        release.write_text("release", encoding="utf-8")
        assert process.process.wait(timeout=2.0) == 0
        assert not recording.exists()
    finally:
        release.write_text("release", encoding="utf-8")
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        process.close()


def test_finalize_wrapper_records_a_false_finalization_result() -> None:
    class IncompleteReader:
        recording_incomplete = False

        def finalize_recording(self, timeout: float) -> bool:
            return False

        def mark_incomplete(self) -> None:
            self.recording_incomplete = True

    process = PtyProcess.__new__(PtyProcess)
    process._reader = IncompleteReader()
    process._recording_incomplete = False

    assert process.finalize_recording(timeout=0.0) is False
    assert process.recording_incomplete is True


def test_finalize_bounds_a_stuck_reader_and_records_the_timeout(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "fake-provider"
    binary.write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    reader_started = threading.Event()
    allow_reader = threading.Event()
    original_run = threading.Thread.run

    def gated_run(thread: threading.Thread) -> None:
        if thread.name.startswith("pty-reader-"):
            reader_started.set()
            allow_reader.wait()
        original_run(thread)

    monkeypatch.setattr(threading.Thread, "run", gated_run)
    process = PtyProcess(
        [str(binary)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TERM": "xterm-256color"},
        recording=tmp_path / "terminal.tty",
    )
    reader = process._reader
    reader_fd = reader._reader_fd
    master_fd = process.master
    try:
        assert reader_started.wait(timeout=2.0)
        started = time.monotonic()
        assert process.finalize_recording(timeout=0.05) is False
        assert time.monotonic() - started < 2.5
        assert process.reader_join_timed_out is True
        assert process.reader_eof_reached is False
        assert process.recording_incomplete is True
        assert not process.recording.exists()
        assert process.master == master_fd
        assert reader_fd >= 0 and reader_fd != master_fd
        os.fstat(master_fd)
        os.fstat(reader_fd)

        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        process.close()
        assert process.master == -1
        allow_reader.set()
        assert reader.join(timeout=2.0) is True
        assert reader.closed is True
        assert reader._reader_fd == -1
        assert reader._wake_read_fd == -1
        assert reader._wake_write_fd == -1
    finally:
        allow_reader.set()
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        process.close()
        reader.join(timeout=2.0)


def test_finalize_drops_a_recording_when_its_writer_lock_stalls(tmp_path: Path) -> None:
    binary = tmp_path / "fake-provider"
    binary.write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    recording = tmp_path / "terminal.tty"
    process = PtyProcess(
        [str(binary)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TERM": "xterm-256color"},
        recording=recording,
    )
    process._reader._recording_lock.acquire()
    try:
        started = time.monotonic()
        assert process.finalize_recording(timeout=0.05) is False
        assert time.monotonic() - started < 2.5
        assert process.recording_lock_timed_out is True
        assert process.recording_incomplete is True
        assert not recording.exists()
    finally:
        process._reader._recording_lock.release()
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        process.close()


def test_reader_thread_start_failure_rolls_back_owned_pty_resources(tmp_path: Path, monkeypatch) -> None:
    recording = tmp_path / "terminal.tty"
    reader_owners = []
    launched_processes = []
    baseline = len(os.listdir("/dev/fd"))
    original_popen = live_session_toolkit.subprocess.Popen

    def track_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        launched_processes.append(process)
        return process

    def fail_thread_start(thread: threading.Thread) -> None:
        reader_owners.append(thread._target.__self__)
        raise RuntimeError("injected reader thread start failure")

    monkeypatch.setattr(live_session_toolkit.subprocess, "Popen", track_popen)
    monkeypatch.setattr(threading.Thread, "start", fail_thread_start)
    with pytest.raises(RuntimeError, match="injected reader thread start failure"):
        PtyProcess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env={"PATH": os.defpath},
            recording=recording,
        )

    assert len(reader_owners) == 1
    assert len(launched_processes) == 1
    assert launched_processes[0].poll() is not None
    owner = reader_owners[0]
    assert owner.closed is True
    assert owner._reader_fd == -1
    assert owner._wake_read_fd == -1
    assert owner._wake_write_fd == -1
    assert owner._stream is None
    assert not recording.exists()
    assert len(os.listdir("/dev/fd")) == baseline


def test_process_cleanup_records_reader_error_without_raising(tmp_path: Path) -> None:
    binary = tmp_path / "fake-provider"
    binary.write_text("#!/usr/bin/env python3\nimport time; time.sleep(30)\n", encoding="utf-8")
    binary.chmod(0o755)
    process = PtyProcess(
        [str(binary)],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        recording=tmp_path / "terminal.tty",
    )
    process._reader._reader_error = OSError("injected reader failure")
    try:
        receipt = live_session_toolkit.cleanup_processes(SPECS["claude"], (process,), [])

        assert process.process.poll() is not None
        assert process.reader_error == "OSError: injected reader failure"
        assert receipt["processes"][0]["reader_error"] == "OSError: injected reader failure"
    finally:
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        if not process._closed:
            try:
                process.close()
            except Exception:
                pass
