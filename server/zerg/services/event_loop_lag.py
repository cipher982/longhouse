"""Measure event-loop lag across a Runtime Host cutover window.

A pending candidate is probed by the deployer while engines, desktops and
browsers reconnect to it. Anything that blocks this process's event loop in
that window stretches every request at once, and an access log cannot tell
"slow handler" from "handler waited for the loop". This monitor answers that
directly: a 50 ms sleep that wakes late is lag, recorded with its offset from
process start. It runs only from a pending start until shortly after the first
reopen, then logs one summary line and exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Callable

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 0.05
_STALL_MS = 100.0
_MAX_RECORDED_STALLS = 32
_AFTER_REOPEN_SECONDS = 10.0
_MAX_WINDOW_SECONDS = 180.0


class LoopLagMonitor:
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.started_at_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.max_ms = 0.0
        self.stalled_ms = 0.0
        self.samples = 0
        self.stalls: list[tuple[float, float]] = []
        self.stall_count = 0
        self.running = False

    def record(self, woke_at: float, lag_ms: float) -> None:
        self.samples += 1
        self.max_ms = max(self.max_ms, lag_ms)
        if lag_ms >= _STALL_MS:
            self.stall_count += 1
            self.stalled_ms += lag_ms
            if len(self.stalls) < _MAX_RECORDED_STALLS:
                # Offset of the stall's start, so it lines up with request logs.
                self.stalls.append((round((woke_at - self.started_at) * 1000 - lag_ms), round(lag_ms)))

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at_utc,
            "since_start_ms": round((time.monotonic() - self.started_at) * 1000),
            "max_ms": round(self.max_ms, 1),
            "stalls_over_100ms": self.stall_count,
            "stalled_ms": round(self.stalled_ms),
            "stalls": [list(stall) for stall in self.stalls],
        }

    async def run(self, is_open: Callable[[], bool]) -> None:
        self.running = True
        reopened_at: float | None = None
        try:
            while True:
                before = time.monotonic()
                await asyncio.sleep(_INTERVAL_SECONDS)
                woke_at = time.monotonic()
                self.record(woke_at, max(0.0, (woke_at - before - _INTERVAL_SECONDS) * 1000))
                if reopened_at is None and is_open():
                    reopened_at = woke_at
                if reopened_at is not None and woke_at - reopened_at >= _AFTER_REOPEN_SECONDS:
                    break
                if woke_at - self.started_at >= _MAX_WINDOW_SECONDS:
                    break
        finally:
            self.running = False
            summary = {"event": "deploy_window_loop_lag", **self.snapshot()}
            if reopened_at is not None:
                summary["reopened_at_ms"] = round((reopened_at - self.started_at) * 1000)
            logger.info("deploy_window_loop_lag %s", json.dumps(summary, separators=(",", ":")))


_monitor: LoopLagMonitor | None = None


def start_deploy_window_monitor(is_open: Callable[[], bool]) -> asyncio.Task | None:
    """Start the monitor for a pending candidate; one per process."""
    global _monitor
    if _monitor is not None:
        return None
    _monitor = LoopLagMonitor()
    return asyncio.create_task(_monitor.run(is_open), name="deploy-window-loop-lag")


def deploy_window_loop_lag() -> dict[str, Any] | None:
    """Current lag figures while the deploy-window monitor runs, else None."""
    if _monitor is None or not _monitor.running:
        return None
    return _monitor.snapshot()
