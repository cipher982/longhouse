"""Machines directory response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from zerg.utils.time import UTCBaseModel

ControlChannelStatus = Literal["connected", "disconnected"]
LaunchBlockedBy = Literal[
    "control_down",
    "no_codex_support",
    "engine_too_old",
    "auth_failed",
    "runtime_unreachable",
]


class MachineDirectoryEntry(UTCBaseModel):
    device_id: str = Field(..., description="Canonical device id used for routing")
    machine_name: str = Field(..., description="Display label; may equal device_id")
    online: bool = Field(..., description="True iff the control channel is currently connected")
    control_channel_status: ControlChannelStatus = Field(
        ...,
        description="Primitive live-control channel status: connected or disconnected.",
    )
    supports: list[str] = Field(
        default_factory=list,
        description="Capabilities announced by the Machine Agent on its last hello frame. Empty when offline.",
    )
    can_launch_codex: bool = Field(
        ...,
        description="Derived launch readiness for Codex v1. Browser/iOS should gate Start on this field.",
    )
    launchable_providers: list[str] = Field(
        default_factory=list,
        description="Providers this Machine Agent can remote-launch now, derived from live supports[].",
    )
    launch_blocked_by: LaunchBlockedBy | None = Field(
        default=None,
        description="Machine-readable reason Codex launch is unavailable; null when can_launch_codex is true.",
    )
    last_seen_at: datetime | None = Field(
        default=None,
        description="Most recent control-channel activity or device-token use; null if never observed.",
    )
    engine_build: str | None = Field(
        default=None,
        description="Engine build string from the last hello frame; null when offline.",
    )


class MachineDirectoryResponse(UTCBaseModel):
    machines: list[MachineDirectoryEntry] = Field(default_factory=list)
