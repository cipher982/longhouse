"""Regression tests for the shared bounded PTY command wrapper."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "run-in-pty.py"
_SPEC = importlib.util.spec_from_file_location("run_in_pty", SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_drain_has_an_absolute_bound() -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    writer = threading.Thread(target=lambda: _write_forever(write_fd, stop))
    writer.start()
    saved_stdout = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    started = time.monotonic()
    try:
        _MODULE._drain(read_fd, quiet_for=0.01, max_duration=0.1)
    finally:
        elapsed = time.monotonic() - started
        os.dup2(saved_stdout, 1)
        os.close(devnull)
        stop.set()
        # Closing the read end makes the blocked writer observe EPIPE before
        # the write end is closed and its fd can be reused.
        os.close(read_fd)
        os.close(write_fd)
        writer.join(timeout=1)
    assert not writer.is_alive()
    assert elapsed < 0.5


def _write_forever(fd: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            os.write(fd, b"x" * 4096)
        except OSError:
            return


def test_timeout_returns_when_stdout_consumer_stalls(tmp_path: Path) -> None:
    leader_pid_path = tmp_path / "leader.pid"
    leader_code = (
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True:\n"
        "    os.write(1, b'x' * 65536)\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--timeout",
            "0.2",
            sys.executable,
            "-c",
            leader_code,
            str(leader_pid_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            exit_code = process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
            pytest.fail("run-in-pty blocked on an unread stdout pipe")
        assert exit_code == 124
    finally:
        process.communicate(timeout=1)
        try:
            leader_pid = int(leader_pid_path.read_text())
        except (FileNotFoundError, ValueError):
            leader_pid = None
        if leader_pid is not None:
            try:
                os.killpg(leader_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(leader_pid, signal.SIGKILL)


def test_timeout_kills_owned_process_group_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-survived"
    descendant_code = (
        "import pathlib, signal, sys, time; "
        "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        "time.sleep(1); pathlib.Path(sys.argv[1]).write_text('alive')"
    )
    leader_code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "time.sleep(30)"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--timeout",
            "0.2",
            sys.executable,
            "-c",
            leader_code,
            descendant_code,
            str(marker),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 124, result.stderr
    time.sleep(1.2)
    assert not marker.exists(), result.stdout


def test_timeout_drain_has_an_absolute_bound(tmp_path: Path) -> None:
    marker_path = tmp_path / "chatty.pid"
    leader_pid_path = tmp_path / "leader.pid"
    chatty_code = (
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "data = b'x' * 4096\n"
        "while True:\n"
        "    os.write(1, data)\n"
        "    time.sleep(0.01)\n"
    )
    leader_code = (
        "import os, pathlib, subprocess, sys, time\n"
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[3]], start_new_session=True)\n"
        "deadline = time.time() + 2\n"
        "while time.time() < deadline and not pathlib.Path(sys.argv[3]).exists():\n"
        "    time.sleep(0.01)\n"
        "time.sleep(30)\n"
    )

    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--timeout",
                "2",
                sys.executable,
                "-c",
                leader_code,
                chatty_code,
                str(leader_pid_path),
                str(marker_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        elapsed = time.monotonic() - started
        assert result.returncode == 124, result.stderr
        assert marker_path.exists(), "chatty descendant did not start"
        assert leader_pid_path.exists(), "PTY leader did not start"
        assert int(marker_path.read_text()) != int(leader_pid_path.read_text())
        assert elapsed < 3.5, f"chatty PTY timeout took {elapsed:.2f}s"
    finally:
        for pid_path in (leader_pid_path, marker_path):
            try:
                pid = int(pid_path.read_text())
            except (FileNotFoundError, ValueError):
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(pid, signal.SIGKILL)
            pid_path.unlink(missing_ok=True)


def test_natural_exit_kills_owned_process_group_descendants(tmp_path: Path) -> None:
    started = tmp_path / "natural-descendant-started"
    marker = tmp_path / "natural-descendant-survived"
    pid_path = tmp_path / "natural-descendant.pid"
    descendant_code = (
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text('started')\n"
        "pathlib.Path(sys.argv[3]).write_text(str(os.getpid()))\n"
        "time.sleep(1)\n"
        "pathlib.Path(sys.argv[2]).write_text('alive')\n"
    )
    leader_code = (
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]])\n"
        "deadline = time.time() + 2\n"
        "while time.time() < deadline and not pathlib.Path(sys.argv[2]).exists():\n"
        "    time.sleep(0.01)\n"
    )

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                sys.executable,
                "-c",
                leader_code,
                descendant_code,
                str(started),
                str(marker),
                str(pid_path),
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )

        assert result.returncode == 0, result.stderr
        assert started.exists(), "descendant did not start before the leader exited"
        time.sleep(1.2)
        assert not marker.exists(), result.stdout
    finally:
        try:
            descendant_pid = int(pid_path.read_text())
        except (FileNotFoundError, ValueError):
            descendant_pid = None
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
