"""Machines directory response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from zerg.utils.time import UTCBaseModel


class MachineDirectoryEntry(UTCBaseModel):
    device_id: str = Field(..., description="Canonical device id used for routing")
    machine_name: str = Field(..., description="Display label; may equal device_id")
    online: bool = Field(..., description="True iff the control channel is currently connected")
    supports: list[str] = Field(
        default_factory=list,
        description="Capabilities announced by the Machine Agent on its last hello frame. Empty when offline.",
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
