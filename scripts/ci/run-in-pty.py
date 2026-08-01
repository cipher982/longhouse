#!/usr/bin/env python3
"""Run a command under a real PTY and preserve its exit status."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time


def _write_all(fd: int, data: bytes) -> None:
    while data:
        written = os.write(fd, data)
        data = data[written:]


def _drain(master: int, *, quiet_for: float = 0.1) -> None:
    deadline = time.monotonic() + quiet_for
    while time.monotonic() < deadline:
        wait_for = max(0.0, min(0.05, deadline - time.monotonic()))
        ready, _, _ = select.select([master], [], [], wait_for)
        if not ready:
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            return
        if not data:
            return
        _write_all(1, data)
        deadline = time.monotonic() + quiet_for


def main() -> int:
    argv = sys.argv[1:]
    timeout = None
    if len(argv) >= 2 and argv[0] == "--timeout":
        timeout = float(argv[1])
        argv = argv[2:]
    if not argv:
        raise SystemExit("usage: run-in-pty.py [--timeout SECONDS] COMMAND [ARG ...]")

    # Do not use pty.spawn here. On macOS it makes the command the session
    # leader of this outer PTY, which changes nested forkpty teardown behavior.
    # Longhouse only needs TTY-backed stdio for these product smokes.
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    finally:
        os.close(slave)

    deadline = time.monotonic() + timeout if timeout is not None else None
    stdin_open = True
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                process.kill()
                process.wait()
                _drain(master)
                return 124

            watched = [master]
            if stdin_open:
                watched.append(0)
            ready, _, _ = select.select(watched, [], [], 0.1)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if data:
                    _write_all(1, data)
            if stdin_open and 0 in ready:
                data = os.read(0, 8192)
                if data:
                    _write_all(master, data)
                else:
                    stdin_open = False
            exit_code = process.poll()
            if exit_code is not None:
                _drain(master)
                return exit_code if exit_code >= 0 else 128 - exit_code
    finally:
        os.close(master)


if __name__ == "__main__":
    raise SystemExit(main())
