"""Machines directory response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import Field

from zerg.utils.time import UTCBaseModel

ProviderLiveProofProvider = Literal["codex", "claude", "opencode", "antigravity"]

ControlChannelStatus = Literal["connected", "disconnected"]
LaunchBlockedBy = Literal[
    "control_down",
    "no_codex_support",
    "no_launch_support",
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
    control_operations_by_provider: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Live Machine Agent operations by provider, derived from supports[]. Empty when offline.",
    )
    can_launch_codex: bool = Field(
        ...,
        description=("Compatibility flag for Codex launch readiness. Prefer launchable_providers for provider-agnostic launch."),
    )
    launchable_providers: list[str] = Field(
        default_factory=list,
        description="Providers this Machine Agent can remote-launch now, derived from live supports[].",
    )
    launch_blocked_by: LaunchBlockedBy | None = Field(
        default=None,
        description="Machine-readable reason no provider can be launched; null when launchable_providers is non-empty.",
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


class ProviderLiveProofRequest(UTCBaseModel):
    provider: ProviderLiveProofProvider = Field(..., description="Provider CLI to prove on the target machine.")
    run_live_token_contract: bool = Field(
        default=False,
        description="When true, spend provider/model calls to prove token-backed behavior.",
    )
    publish: bool = Field(
        default=True,
        description="Publish the proof into the machine's stable local sidecar before returning it.",
    )
    live_token_timeout_secs: int = Field(
        default=120,
        ge=1,
        le=600,
        description="Provider turn timeout passed to the live-token canary lane.",
    )
    timeout_secs: int | None = Field(
        default=None,
        ge=1,
        le=900,
        description=("Optional provider-live process timeout. When omitted, the Machine Agent uses a provider-aware default."),
    )


class ProviderLiveProofResponse(UTCBaseModel):
    device_id: str = Field(..., description="Machine that executed the proof.")
    provider: ProviderLiveProofProvider = Field(..., description="Provider that was proved.")
    command_id: str = Field(..., description="Machine-control command id.")
    result: dict[str, Any] = Field(..., description="Structured provider-live result returned by the Machine Agent.")
