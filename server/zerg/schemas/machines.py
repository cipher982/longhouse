"""Machines directory response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import ConfigDict
from pydantic import Field

from zerg.utils.time import UTCBaseModel

ProviderLiveProofProvider = Literal["claude", "opencode", "antigravity"]
ArchiveBacklogControlMode = Literal["paused", "trickle", "drain"]
MachineControlOperationStatus = Literal["queued", "running", "succeeded", "failed", "timed_out"]
RemoteExecutionLifetime = Literal["one_shot", "live_control"]

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
        description=("Compatibility flag for Codex launch readiness. " "Prefer launchable_providers for provider-agnostic launch."),
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
    launch: "MachineLaunchProjection" = Field(
        ...,
        description="Canonical Console launch options and defaults for human clients.",
    )


class MachineLaunchProviderOption(UTCBaseModel):
    provider: str = Field(..., description="Provider identifier.")
    execution_lifetimes: list[RemoteExecutionLifetime] = Field(
        ...,
        description="Execution modes this machine can launch for the provider now.",
    )


class MachineLaunchProjection(UTCBaseModel):
    blocked_by: LaunchBlockedBy | None = Field(
        default=None,
        description="Reason no Console launch option is available; null when providers is non-empty.",
    )
    providers: list[MachineLaunchProviderOption] = Field(...)
    default_provider: str | None = None
    default_execution_lifetime: RemoteExecutionLifetime | None = None


class MachineDirectoryResponse(UTCBaseModel):
    machines: list[MachineDirectoryEntry] = Field(default_factory=list)


class WorkspaceSuggestion(UTCBaseModel):
    path: str = Field(..., description="Absolute working directory on the target machine.")
    label: str = Field(..., description="Display label: git repo+branch when known, else compact path.")
    git_repo: str | None = Field(default=None, description="Git remote URL of the most-recent session in this cwd.")
    git_branch: str | None = Field(default=None, description="Git branch of the most-recent session in this cwd.")
    score: float = Field(..., description="Frecency score (frequency weighted by recency); higher ranks first.")
    last_used_at: datetime | None = Field(default=None, description="Most recent activity in this cwd on this machine.")
    session_count: int = Field(..., description="Sessions launched in this cwd within the lookback window.")


class WorkspaceSuggestionsResponse(UTCBaseModel):
    device_id: str = Field(..., description="Machine the suggestions are scoped to.")
    workspaces: list[WorkspaceSuggestion] = Field(default_factory=list)


class ProviderLiveProofRequest(UTCBaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderLiveProofProvider = Field(..., description="Provider CLI to prove on the target machine.")
    expected_provider_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="Optional release/version the returned provider-live artifact must prove.",
    )
    publish: bool = Field(
        default=True,
        description="Publish the proof into the machine's stable local sidecar before returning it.",
    )
    timeout_secs: int | None = Field(
        default=None,
        ge=1,
        le=900,
        description="Optional provider-live process timeout. When omitted, the Machine Agent uses a no-token default.",
    )
    run_live_token_contract: bool = Field(
        default=False,
        description="Run the provider-specific live-token contract when the target Machine Agent supports it.",
    )
    live_token_timeout_secs: int | None = Field(
        default=None,
        ge=1,
        le=600,
        description="Optional timeout for the live-token contract portion of the proof.",
    )


class ProviderLiveProofAcceptedResponse(UTCBaseModel):
    operation_id: str = Field(..., description="Durable machine-control operation id.")
    status: MachineControlOperationStatus = Field(..., description="Current operation state.")
    status_url: str = Field(..., description="Relative API URL for polling operation status.")
    device_id: str = Field(..., description="Machine that accepted the proof command.")
    provider: ProviderLiveProofProvider = Field(..., description="Provider that will be proved.")


class MachineControlOperationResponse(UTCBaseModel):
    operation_id: str = Field(..., description="Durable machine-control operation id.")
    device_id: str = Field(..., description="Target machine id.")
    command_type: str = Field(..., description="Machine Agent command type.")
    command_id: str = Field(..., description="Machine Agent command id.")
    provider: str | None = Field(default=None, description="Provider scoped by the operation, if any.")
    status: MachineControlOperationStatus = Field(..., description="Current operation state.")
    request: dict[str, Any] = Field(default_factory=dict, description="Operation request payload.")
    result: dict[str, Any] | None = Field(default=None, description="Machine Agent result when succeeded.")
    error: dict[str, Any] | None = Field(default=None, description="Machine Agent error when failed or timed out.")
    created_at: datetime = Field(..., description="Operation creation time.")
    started_at: datetime | None = Field(default=None, description="Dispatch start time.")
    finished_at: datetime | None = Field(default=None, description="Terminal completion time.")
    timeout_secs: int = Field(..., description="Operation lease in seconds.")


class ArchiveBacklogResponse(UTCBaseModel):
    device_id: str = Field(..., description="Machine whose archive backlog was inspected.")
    archive_repair: dict[str, Any] = Field(default_factory=dict)


class ArchiveBacklogControlRequest(UTCBaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ArchiveBacklogControlMode = Field(..., description="Archive repair mode to apply on the Machine Agent.")
    max_tick_bytes: int | None = Field(
        default=None,
        ge=1,
        description="Optional per-tick byte budget consumed by the Machine Agent archive scheduler.",
    )
    include_huge: bool = Field(
        default=False,
        description="Allow replaying archive ranges >=100MB in explicit drain mode.",
    )
    lease_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Expiry for trickle/drain control; ignored for paused mode.",
    )
    timeout_secs: int | None = Field(
        default=None,
        ge=1,
        le=60,
        description="Machine-control command timeout.",
    )


class ArchiveBacklogControlResponse(UTCBaseModel):
    device_id: str = Field(..., description="Machine that received the archive control command.")
    command_id: str = Field(..., description="Machine-control command id.")
    result: dict[str, Any] = Field(default_factory=dict)
