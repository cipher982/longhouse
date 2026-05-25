"""Canonical remote-launch lifecycle projection.

The durable source of truth is ``SessionLaunchAttempt``. Legacy
``AgentSession.launch_*`` fields are compatibility write shims and must not be
read for product state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from zerg.models.agents import SessionLaunchAttempt

RemoteLaunchLifecycleState = Literal[
    "launching",
    "live",
    "launching_unknown",
    "launch_failed",
    "launch_orphaned",
]


@dataclass(frozen=True)
class RemoteLaunchLifecycle:
    state: RemoteLaunchLifecycleState
    error_code: str | None
    error_message: str | None
    lease_until: datetime | None


def project_remote_launch_lifecycle(attempt: SessionLaunchAttempt | None) -> RemoteLaunchLifecycle | None:
    """Project a launch attempt into the user-visible lifecycle contract."""

    if attempt is None:
        return None

    raw_state = str(attempt.state or "").strip()
    if raw_state == "failed":
        state: RemoteLaunchLifecycleState = "launch_failed"
    elif raw_state == "abandoned":
        state = "launch_orphaned"
    elif attempt.run_id is not None or raw_state == "adopted":
        state = "live"
    elif raw_state == "dispatched":
        state = "launching_unknown"
    else:
        state = "launching"

    return RemoteLaunchLifecycle(
        state=state,
        error_code=attempt.error_code,
        error_message=attempt.error_message,
        lease_until=attempt.expires_at,
    )


__all__ = [
    "RemoteLaunchLifecycle",
    "RemoteLaunchLifecycleState",
    "project_remote_launch_lifecycle",
]
