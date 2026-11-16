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
    """A monotonic-clock deadline (ns) that joining callers can only extend; None never expires.

    Once it has fired it stays fired: a caller that arrives while the
    interrupted read is still unwinding cannot revive it, and must start a
    read of its own (``extend`` returns False).
    """

    __slots__ = ("deadline_ns", "tripped", "_lock")

    def __init__(self, deadline_ns: int | None) -> None:
        self.deadline_ns = deadline_ns
        self.tripped = False
        self._lock = threading.Lock()

    def extend(self, deadline_ns: int | None) -> bool:
        """Keep the read alive for a later caller; False once it has fired."""

        with self._lock:
            if self.tripped:
                return False
            if self.deadline_ns is not None and (deadline_ns is None or deadline_ns > self.deadline_ns):
                self.deadline_ns = deadline_ns
            return True

    def trip_if_expired(self) -> bool:
        """True once the deadline has passed; from then on it never un-fires."""

        if self.tripped:
            return True
        deadline_ns = self.deadline_ns
        if deadline_ns is None or time.monotonic_ns() <= deadline_ns:
            return False
        with self._lock:
            # Re-check under the lock: a caller may have extended it meanwhile.
            if self.deadline_ns is not None and time.monotonic_ns() > self.deadline_ns:
                self.tripped = True
            return self.tripped


@contextmanager
def bound_read_deadline(deadline: ReadDeadline) -> Iterator[None]:
    previous = getattr(_bound, "deadline", None)
    _bound.deadline = deadline
    try:
        yield
    finally:
        _bound.deadline = previous


def interrupt_expired_read() -> int:
    """SQLite progress handler: nonzero interrupts the running statement.

    It interrupts once, when the deadline fires. After that it lets the read's
    own cleanup (its ROLLBACK) run instead of interrupting that too.
    """

    deadline = getattr(_bound, "deadline", None)
    if deadline is None or deadline.tripped:
        return 0
    return 1 if deadline.trip_if_expired() else 0
