"""Pydantic schemas for Runners API.

These schemas define the request/response models for the runners API
endpoints used to manage runner enrollment, configuration, and jobs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from zerg.utils.time import UTCBaseModel

# ---------------------------------------------------------------------------
# Runner Enrollment
# ---------------------------------------------------------------------------


class EnrollTokenCreate(BaseModel):
    """Request to create a new enrollment token."""

    pass  # No parameters needed for v1


class EnrollTokenResponse(UTCBaseModel):
    """Response containing enrollment token and setup instructions."""

    enroll_token: str = Field(..., description="One-time enrollment token")
    expires_at: datetime = Field(..., description="Token expiration timestamp")
    longhouse_url: str = Field(..., description="Longhouse API URL for runner connection")
    docker_command: str = Field(..., description="Complete docker run command for easy setup")
    one_liner_install_command: str = Field(..., description="One-liner curl command for automated install")


class RunnerRegisterRequest(BaseModel):
    """Request to register a new runner using an enrollment token."""

    enroll_token: str = Field(..., description="One-time enrollment token")
    name: Optional[str] = Field(None, description="Optional runner name (auto-generated if not provided)")
    labels: Optional[dict[str, str]] = Field(None, description="Optional labels for runner targeting")
    capabilities: Optional[list[str]] = Field(None, description="Optional capabilities for the new runner")
    metadata: Optional[dict[str, Any]] = Field(None, description="Runner metadata (hostname, os, arch, etc.)")


class RunnerRegisterResponse(BaseModel):
    """Response after successful runner registration."""

    runner_id: int = Field(..., description="Unique runner ID")
    runner_secret: str = Field(..., description="Long-lived secret for runner authentication (store securely!)")
    name: str = Field(..., description="Runner name")
    runner_capabilities_csv: str = Field(
        default="exec.readonly",
        description="Comma-separated runner capabilities for installer env files",
    )


class RunnerPreflightRequest(BaseModel):
    """Unauthenticated runner credential check used by the local doctor."""

    runner_id: Optional[int] = Field(None, description="Runner ID if known")
    runner_name: Optional[str] = Field(None, description="Runner name if RUNNER_ID is not configured")
    secret: str = Field(..., description="Runner secret used for websocket authentication")


class RunnerPreflightResponse(UTCBaseModel):
    """Structured result from runner credential preflight."""

    authenticated: bool
    reason_code: str
    summary: str
    runner_id: Optional[int] = None
    runner_name: Optional[str] = None
    status: Optional[str] = None
    status_reason: Optional[str] = None
    status_summary: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    last_seen_age_seconds: Optional[int] = None
    install_mode: Optional[str] = None
    runner_version: Optional[str] = None
    latest_runner_version: Optional[str] = None
    version_status: Optional[str] = None
    capabilities_match: Optional[bool] = None


# ---------------------------------------------------------------------------
# Runner Management
# ---------------------------------------------------------------------------


class RunnerResponse(UTCBaseModel):
    """Response model for a single runner."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    name: str
    labels: Optional[dict[str, str]] = None
    capabilities: list[str] = Field(default_factory=lambda: ["exec.readonly"])
    status: str  # online|offline|revoked
    status_reason: Optional[str] = None
    status_summary: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    last_seen_age_seconds: Optional[int] = None
    heartbeat_interval_ms: int = Field(default=30_000, description="Reported heartbeat interval in milliseconds")
    stale_after_seconds: int = Field(default=90, description="Seconds after which a heartbeat is treated as stale")
    runner_metadata: Optional[dict[str, Any]] = None
    install_mode: Optional[str] = None
    runner_version: Optional[str] = None
    latest_runner_version: Optional[str] = None
    version_status: str = Field(default="unknown", description="current|outdated|ahead|unknown")
    reported_capabilities: Optional[list[str]] = None
    capabilities_match: Optional[bool] = None
    created_at: datetime
    updated_at: datetime


class RunnerUpdate(BaseModel):
    """Request to update a runner's configuration."""

    name: Optional[str] = Field(None, description="New runner name")
    labels: Optional[dict[str, str]] = Field(None, description="New labels")
    capabilities: Optional[list[str]] = Field(None, description="New capabilities")


class RunnerListResponse(BaseModel):
    """Response for listing runners."""

    runners: list[RunnerResponse] = Field(..., description="List of runners")


# ---------------------------------------------------------------------------
# Runner Jobs (for audit/history)
# ---------------------------------------------------------------------------


class RunnerJobResponse(UTCBaseModel):
    """Response model for a runner job."""

    model_config = ConfigDict(from_attributes=True)

    id: str  # UUID
    owner_id: int
    commis_id: Optional[str] = None
    run_id: Optional[str] = None
    runner_id: int
    command: str
    timeout_secs: int
    status: str  # queued|running|success|failed|timeout|canceled
    exit_code: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    stdout_trunc: Optional[str] = None
    stderr_trunc: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime


class RunnerJobListResponse(BaseModel):
    """Response for listing recent runner jobs."""

    jobs: list[RunnerJobResponse] = Field(..., description="Recent jobs for this runner")


# ---------------------------------------------------------------------------
# Secret Rotation
# ---------------------------------------------------------------------------


class RunnerRotateSecretResponse(BaseModel):
    """Response after rotating a runner's secret."""

    runner_id: int = Field(..., description="Runner ID")
    runner_secret: str = Field(
        ...,
        description="New long-lived secret for runner authentication (store securely!)",
    )
    message: str = Field(
        default="Secret rotated successfully. Update your runner configuration.",
        description="Operation status message",
    )


# ---------------------------------------------------------------------------
# Runner Status Summary
# ---------------------------------------------------------------------------


class RunnerStatusItem(BaseModel):
    """Individual runner status for summary."""

    name: str
    status: str  # online|offline|revoked
    status_reason: Optional[str] = None
    status_summary: Optional[str] = None


class RunnerStatusResponse(BaseModel):
    """Summary of runner health for UI status indicators."""

    total: int = Field(..., description="Total number of runners")
    online: int = Field(..., description="Number of online runners")
    offline: int = Field(..., description="Number of offline runners")
    runners: list[RunnerStatusItem] = Field(..., description="Status of each runner")


class RunnerDoctorCheck(BaseModel):
    """Named diagnostic check for runner doctor output."""

    key: str
    label: str
    status: str  # ok|warn|fail
    message: str


class RunnerDoctorResponse(BaseModel):
    """Reason-coded doctor response for a single runner."""

    severity: str  # healthy|warning|error
    reason_code: str
    summary: str
    recommended_action: str
    install_mode: Optional[str] = None
    repair_install_mode: Optional[str] = None
    repair_supported: bool = False
    checks: list[RunnerDoctorCheck] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Generic Success Response
# ---------------------------------------------------------------------------


class RunnerSuccessResponse(BaseModel):
    """Generic success response for runner operations."""

    success: bool = True
    message: Optional[str] = None
