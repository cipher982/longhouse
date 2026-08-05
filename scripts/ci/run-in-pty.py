#!/usr/bin/env python3
"""Run a command under a real PTY and preserve its exit status."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time


def _write_all(fd: int, data: bytes, *, deadline: float | None = None) -> bool:
    """Write without letting a stalled pipe defeat the command timeout."""

    try:
        original_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except OSError:
        original_flags = None
    if original_flags is not None:
        fcntl.fcntl(fd, fcntl.F_SETFL, original_flags | os.O_NONBLOCK)
    try:
        while data:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_for = min(0.05, remaining)
            else:
                wait_for = 0.05
            try:
                written = os.write(fd, data)
            except BlockingIOError:
                try:
                    select.select([], [fd], [], wait_for)
                except OSError:
                    return False
                continue
            except OSError:
                return False
            if written <= 0:
                return False
            data = data[written:]
        return True
    finally:
        if original_flags is not None:
            fcntl.fcntl(fd, fcntl.F_SETFL, original_flags)


def _drain(
    master: int, *, quiet_for: float = 0.1, max_duration: float = 0.5
) -> bool:
    drain_deadline = time.monotonic() + max_duration
    quiet_deadline = time.monotonic() + quiet_for
    while time.monotonic() < drain_deadline:
        wait_for = max(
            0.0,
            min(
                0.05,
                drain_deadline - time.monotonic(),
                quiet_deadline - time.monotonic(),
            ),
        )
        if wait_for == 0:
            return True
        ready, _, _ = select.select([master], [], [], wait_for)
        if not ready:
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            return True
        if not data:
            return True
        if not _write_all(1, data, deadline=drain_deadline):
            return False
        quiet_deadline = time.monotonic() + quiet_for
    return True


def _warn_output_failure() -> None:
    _write_all(
        2,
        b"run-in-pty: could not forward all PTY output before the deadline\n",
        deadline=time.monotonic() + 0.1,
    )


def _kill_owned_process_group(
    process: subprocess.Popen[bytes],
    *,
    wait_for: float = 0.5,
    reap: bool = True,
    warn_on_fallback: bool = True,
) -> None:
    """Kill the PTY child and every descendant without waiting forever."""

    try:
        # _attach_controlling_tty calls setsid(), so the child PID is also the
        # private process-group ID. This is the ownership boundary for the
        # command under test; killing only the direct child leaks descendants.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        try:
            process.kill()
        except OSError:
            pass
    except OSError as error:
        if warn_on_fallback:
            print(
                f"warning: process-group cleanup fell back to leader kill: {error}",
                file=sys.stderr,
            )
        try:
            process.kill()
        except OSError:
            pass

    if not reap:
        return

    deadline = time.monotonic() + wait_for
    while process.poll() is None and time.monotonic() < deadline:
        try:
            process.wait(timeout=min(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            continue

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            # The wrapper must return even if the kernel has not made the
            # leader waitable yet. The owned process group was already sent
            # SIGKILL, and launchd/container cleanup can reap the remainder.
            pass


def _attach_controlling_tty(slave: int) -> None:
    """Give the child a real controlling terminal, not only TTY-backed stdio."""

    os.setsid()
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)


def _child_exited_without_reaping(pid: int) -> bool:
    """Peek at child completion while keeping its PID/PGID reserved."""

    result = os.waitid(
        os.P_PID,
        pid,
        os.WEXITED | os.WNOHANG | os.WNOWAIT,
    )
    return result is not None and result.si_pid == pid


def main() -> int:
    argv = sys.argv[1:]
    timeout = None
    if len(argv) >= 2 and argv[0] == "--timeout":
        timeout = float(argv[1])
        argv = argv[2:]
    if not argv:
        raise SystemExit("usage: run-in-pty.py [--timeout SECONDS] COMMAND [ARG ...]")

    # Keep explicit Popen and child-side terminal setup so this helper retains
    # exact process/wait/timeout ownership. The pre-exec function is
    # intentionally limited to setsid/TIOCSCTTY; the child still needs a
    # controlling terminal because managed provider foreground handoff uses
    # tcsetpgrp, not only isatty().
    master, slave = pty.openpty()
    try:
        try:
            process = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                preexec_fn=lambda: _attach_controlling_tty(slave),
            )
        except BaseException:
            os.close(master)
            raise
    finally:
        os.close(slave)

    deadline = time.monotonic() + timeout if timeout is not None else None
    stdin_open = True
    pty_closed = False
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                _kill_owned_process_group(process)
                if not pty_closed:
                    if not _drain(master, max_duration=0.5):
                        _warn_output_failure()
                return 124

            watched = [] if pty_closed else [master]
            if stdin_open:
                watched.append(0)
            ready, _, _ = select.select(watched, [], [], 0.1)
            if not pty_closed and master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    pty_closed = True
                    data = b""
                if data:
                    if not _write_all(1, data, deadline=deadline):
                        _kill_owned_process_group(process)
                        _warn_output_failure()
                        return 124 if deadline is not None else 125
                else:
                    pty_closed = True
            if stdin_open and 0 in ready:
                data = os.read(0, 8192)
                if data:
                    try:
                        input_deadline = deadline or time.monotonic() + 0.5
                        if not _write_all(master, data, deadline=input_deadline):
                            _kill_owned_process_group(process)
                            _warn_output_failure()
                            return 125
                    except OSError:
                        pty_closed = True
                else:
                    stdin_open = False
            if _child_exited_without_reaping(process.pid):
                drain_succeeded = pty_closed or _drain(master, max_duration=0.5)
                _kill_owned_process_group(
                    process, wait_for=0.1, reap=False, warn_on_fallback=False
                )
                exit_code = process.wait(timeout=0.1)
                if not drain_succeeded:
                    _warn_output_failure()
                    return 125
                return exit_code if exit_code >= 0 else 128 - exit_code
    finally:
        os.close(master)


if __name__ == "__main__":
    raise SystemExit(main())
