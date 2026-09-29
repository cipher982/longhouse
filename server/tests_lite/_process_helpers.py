"""Assertions about child processes that a pool's own threads also reap."""

import multiprocessing

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
