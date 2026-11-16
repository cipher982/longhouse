"""Cut a catalog read off once nobody is waiting for it.

A caller that times out cannot cancel SQLite work already running in a worker
thread, so an abandoned read used to hold its slot until SQLite finished, and
the read lane was kept at two slots to cap that waste (catalog-read-path.md).

Each read runs with a ``ReadDeadline`` bound to its worker thread. Every
catalog connection carries a SQLite progress handler that interrupts the
running statement when the deadline bound to the thread running it has
passed. Writer and maintenance threads never bind one, so a write is never
interrupted. A read shared by several callers keeps one deadline, extended to
the latest of theirs, so it is cut only when the last caller has given up.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

# SQLite virtual machine instructions between progress-handler calls. A
# handler call costs well under a microsecond; at this interval an expired
# read stops within about a millisecond of CPU work.
PROGRESS_HANDLER_INSTRUCTIONS = 10_000

_bound = threading.local()


class ReadDeadline:
    """A monotonic-clock deadline (ns) that joining callers can only extend; None never expires."""

    __slots__ = ("deadline_ns", "_lock")

    def __init__(self, deadline_ns: int | None) -> None:
        self.deadline_ns = deadline_ns
        self._lock = threading.Lock()

    def extend(self, deadline_ns: int | None) -> None:
        with self._lock:
            if self.deadline_ns is None:
                return
            if deadline_ns is None or deadline_ns > self.deadline_ns:
                self.deadline_ns = deadline_ns

    def expired(self) -> bool:
        deadline_ns = self.deadline_ns
        return deadline_ns is not None and time.monotonic_ns() > deadline_ns


@contextmanager
def bound_read_deadline(deadline: ReadDeadline) -> Iterator[None]:
    previous = getattr(_bound, "deadline", None)
    _bound.deadline = deadline
    try:
        yield
    finally:
        _bound.deadline = previous


def interrupt_expired_read() -> int:
    """SQLite progress handler: nonzero interrupts the running statement."""

    deadline = getattr(_bound, "deadline", None)
    return 1 if deadline is not None and deadline.expired() else 0
