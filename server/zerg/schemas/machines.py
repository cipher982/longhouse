"""Machines directory response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from zerg.utils.time import UTCBaseModel

ArchiveBacklogControlMode = Literal["paused", "trickle", "drain"]

ControlChannelStatus = Literal["connected", "disconnected"]
LaunchBlockedBy = Literal[
    "control_down",
    "no_launch_support",
    "engine_too_old",
    "auth_failed",
    "runtime_unreachable",
    "providers_not_ready",
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
    last_seen_at: datetime | None = Field(
        default=None,
        description="Most recent control-channel activity or device-token use; null if never observed.",
    )
    engine_build: str | None = Field(
        default=None,
        description="Engine build string from the last hello frame; null when offline.",
    )
    connected_since: datetime | None = Field(
        default=None,
        description=(
            "When the current control channel connected; null when offline. Paired with "
            "last_seen_at this separates a machine that has held one connection from one "
            "that keeps reconnecting."
        ),
    )
    provider_readiness: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Per-provider readiness reported by the Machine Agent: state is one of ready, "
            "cli_missing, not_authenticated, unknown, with optional detail, "
            "credential_override, and remediation. Empty means not reported -- an offline "
            "machine or an older engine -- never that providers are unready."
        ),
    )
    launch: "MachineLaunchProjection" = Field(
        ...,
        description="Canonical Console launch options and defaults for human clients.",
    )


class MachineLaunchProviderOption(UTCBaseModel):
    provider: str = Field(..., description="Provider identifier.")


class MachineLaunchUnavailableProvider(UTCBaseModel):
    provider: str = Field(..., description="Provider identifier.")
    reason: Literal["not_authenticated", "cli_missing"] = Field(
        ..., description="Engine readiness state that makes a Console turn fail before any work."
    )
    remediation: str | None = Field(default=None, description="Human instruction, e.g. 'Sign in to codex on this machine'.")


class MachineLaunchProjection(UTCBaseModel):
    blocked_by: LaunchBlockedBy | None = Field(
        default=None,
        description="Reason no Console launch option is available; null when providers is non-empty.",
    )
    providers: list[MachineLaunchProviderOption] = Field(...)
    default_provider: str | None = None
    unavailable_providers: list[MachineLaunchUnavailableProvider] = Field(
        default_factory=list,
        description=(
            "Providers this machine's engine can drive whose readiness says a turn would fail now "
            "(signed out, CLI missing). Never launchable; shown so the user knows what to fix."
        ),
    )


class MachineDirectoryResponse(UTCBaseModel):
    machines: list[MachineDirectoryEntry] = Field(default_factory=list)


class MachineSessionBrief(UTCBaseModel):
    session_id: str
    title: str
    project: str | None = None
    provider: str | None = None
    last_activity_at: datetime | None = None
    activity_state: str | None = Field(default=None, description="Served activity axis: idle, thinking, executing, ...")


class MachineActivityDay(UTCBaseModel):
    date: str = Field(..., description="Local calendar day (YYYY-MM-DD) in the requested UTC offset.")
    total: int
    by_provider: dict[str, int] = Field(..., description="Sessions started that day, by provider.")


class MachineProjectCount(UTCBaseModel):
    project: str
    sessions: int


class MachineActivity(UTCBaseModel):
    sessions_started: int = Field(..., description="Sessions started on this machine inside the window (default timeline visibility).")
    daily: list[MachineActivityDay] = Field(..., description="Exactly `days` entries, oldest first, zero-filled.")
    top_projects: list[MachineProjectCount] = Field(..., description="Up to three projects by sessions started.")
    latest_session: MachineSessionBrief | None = None
    live_count: int = Field(..., description="Sessions on this machine the Timeline shows under Live now (working_set open).")
    live_sessions: list[MachineSessionBrief] = Field(..., description="Up to five, most recent first.")


class MachineHistorySync(UTCBaseModel):
    state: str = Field(..., description="History import state reported by the Machine Agent (current, importing, ...).")
    source_count: int | None = None
    remaining_bytes: int | None = None
    remaining_records: int | None = None
    acknowledged_records: int | None = None


class MachineSync(UTCBaseModel):
    reported_at: datetime = Field(..., description="When the Runtime Host received the latest shipping heartbeat.")
    report_age_seconds: int
    stale: bool
    status: Literal["healthy", "degraded", "broken", "offline", "unknown"]
    status_summary: str
    engine_version: str | None = None
    last_upload_at: datetime | None = None
    upload_p95_ms: int | None = None
    waiting_uploads: int | None = None
    failed_uploads: int | None = None
    history: MachineHistorySync


class MachineSummary(UTCBaseModel):
    machine: MachineDirectoryEntry
    activity: MachineActivity
    sync: MachineSync | None = Field(default=None, description="Null when no shipping heartbeat is on record in the last 30 days.")


class MachinesSummaryResponse(UTCBaseModel):
    generated_at: datetime
    days: int
    utc_offset_minutes: int
    first_day: str
    last_day: str
    machines: list[MachineSummary]


class MachineRenameRequest(UTCBaseModel):
    machine_name: str = Field(..., min_length=1, max_length=255, description="Durable human-facing machine label.")


class MachineRenameResponse(UTCBaseModel):
    device_id: str
    machine_name: str
    changed: bool


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


class RecentModel(UTCBaseModel):
    model: str = Field(..., description="Provider model id, preserving the provider's exact casing.")
    last_used_at: datetime = Field(..., description="When this model was last reported by a completed turn.")
    label: str | None = Field(
        None,
        description="Short display name ('opus 5.5'), the same naming as usage_latest.label; clients render it verbatim.",
    )

    @model_validator(mode="after")
    def _derive_label(self) -> "RecentModel":
        if self.label is None:
            # Lazy: the services package imports schemas.
            from zerg.services.session_provider_facts import short_model_name

            self.label = short_model_name(self.model)
        return self


class RecentModelsResponse(UTCBaseModel):
    device_id: str = Field(..., description="Machine the models are scoped to.")
    provider: str = Field(..., description="Provider whose usage facts supplied the models.")
    days_back: int = Field(..., description="Lookback window used to select sessions.")
    models: list[RecentModel] = Field(default_factory=list)


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
