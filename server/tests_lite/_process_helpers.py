"""Assertions about child processes that a pool's own threads also reap."""

import multiprocessing
import os
import signal
import time
from pathlib import Path

import psutil


def child_is_gone(process: multiprocessing.Process) -> bool:
    """True once the child is dead, whoever reaped it.

    `Process.is_alive()` is False only if *this handle's* `waitpid` collected the
    exit. A `ProcessPoolExecutor`'s manager thread joins the same child, and
    whichever thread loses the `waitpid` race gets ECHILD, after which the loser's
    `is_alive()` reports True forever for a process the kernel no longer has. Under
    CPU contention the loser was the test about 1 run in 130. Ask the kernel.
    """
    try:
        return psutil.Process(process.pid).status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except psutil.NoSuchProcess:
        return True


def ignore_sigterm_and_park(marker_dir: str, seconds: float) -> None:
    """Run in a pool child: become SIGTERM-proof, say so, then stay busy.

    A child that ignores SIGTERM is what keeps the stdlib's broken-pool cleanup
    joining forever. SIGSTOP would model it too but Darwin delivers SIGTERM to a
    stopped process, and this works on both.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (Path(marker_dir) / str(os.getpid())).touch()
    time.sleep(seconds)
