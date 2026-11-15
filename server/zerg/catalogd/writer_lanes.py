"""The catalogd writer thread with two lanes: primary work and derived work.

Source ingest, heartbeats, presence, input receipts and every other control
write are primary. Work that can be recomputed from source (render shadow
records, host render commits, projector bookkeeping) is derived. One thread
still performs every write, because SQLite has one writer, but derived work
may never take queue capacity or the thread's turn from primary work:

* admission: derived work is counted against its own small cap, and is refused
  while the primary queue is half full, so a flood of derived calls is rejected
  with a retry hint before it can make a heartbeat wait behind it;
* order: the thread takes primary work first. Derived work runs when no
  primary work waits, plus one item per `FAIR_SHARE` primary items, so a
  permanently busy primary lane cannot starve it forever.

See control-plane `docs/specs/production-storage-lifecycle.md` (derived work
never competes with source work).
"""

from __future__ import annotations

import threading
from collections import deque
from concurrent.futures import Future
from typing import Any
from typing import Callable

PRIMARY = "primary"
DERIVED = "derived"

# One derived item runs after this many consecutive primary items when both
# lanes have work waiting.
FAIR_SHARE = 16


class WriterLanes:
    def __init__(self, *, thread_name: str = "catalogd-sqlite") -> None:
        self._lanes: dict[str, deque[tuple[Future, Callable[[], Any]]]] = {PRIMARY: deque(), DERIVED: deque()}
        self._cond = threading.Condition()
        self._closed = False
        self._primary_streak = 0
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self._thread.start()

    def submit(self, lane: str, fn: Callable[[], Any]) -> Future:
        future: Future = Future()
        with self._cond:
            if self._closed:
                raise RuntimeError("writer lanes are shut down")
            self._lanes[lane].append((future, fn))
            self._cond.notify()
        return future

    def _next(self) -> tuple[Future, Callable[[], Any]] | None:
        with self._cond:
            while True:
                primary, derived = self._lanes[PRIMARY], self._lanes[DERIVED]
                if primary and (not derived or self._primary_streak < FAIR_SHARE):
                    self._primary_streak += 1
                    return primary.popleft()
                if derived:
                    self._primary_streak = 0
                    return derived.popleft()
                if self._closed:
                    return None
                self._cond.wait()

    def _run(self) -> None:
        while True:
            item = self._next()
            if item is None:
                return
            future, fn = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - delivered to the awaiting caller
                future.set_exception(exc)

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._cond:
            self._closed = True
            if cancel_futures:
                for lane in self._lanes.values():
                    while lane:
                        lane.popleft()[0].cancel()
            self._cond.notify_all()
        if wait:
            self._thread.join()
