"""Ops events bridge lifecycle hook.

The retired automation data plane used this bridge to normalize run,
automation, and thread events into an `ops:events` ticker. Keep the lifecycle
object so startup/shutdown wiring stays stable while the launch-era ops surface
has no domain events to subscribe to.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


OPS_TOPIC = "ops:events"


class OpsEventsBridge:
    """No-op bridge retained for app startup/shutdown compatibility."""

    _started: bool = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        logger.info("OpsEventsBridge started with no launch-era event subscriptions")

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False


ops_events_bridge = OpsEventsBridge()
