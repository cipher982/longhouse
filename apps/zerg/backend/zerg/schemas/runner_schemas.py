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


# ---------------------------------------------------------------------------
# Runner Enrollment
# ---------------------------------------------------------------------------


class EnrollTokenCreate(BaseModel):
    """Request to create a new enrollment token."""

    pass  # No parameters needed for v1


class EnrollTokenResponse(BaseModel):
    """Response containing enrollment token and setup instructions."""

    enroll_token: str = Field(..., description="One-time enrollment token")
    expires_at: datetime = Field(..., description="Token expiration timestamp")
    swarmlet_url: str = Field(..., description="Swarmlet API URL for runner connection")
    docker_command: str = Field(..., description="Complete docker run command for easy setup")


class RunnerRegisterRequest(BaseModel):
    """Request to register a new runner using an enrollment token."""

    enroll_token: str = Field(..., description="One-time enrollment token")
    name: Optional[str] = Field(None, description="Optional runner name (auto-generated if not provided)")
    labels: Optional[dict[str, str]] = Field(None, description="Optional labels for runner targeting")
    metadata: Optional[dict[str, Any]] = Field(None, description="Runner metadata (hostname, os, arch, etc.)")


class RunnerRegisterResponse(BaseModel):
    """Response after successful runner registration."""

    runner_id: int = Field(..., description="Unique runner ID")
    runner_secret: str = Field(..., description="Long-lived secret for runner authentication (store securely!)")
    name: str = Field(..., description="Runner name")


# ---------------------------------------------------------------------------
# Runner Management
# ---------------------------------------------------------------------------


class RunnerResponse(BaseModel):
    """Response model for a single runner."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    name: str
    labels: Optional[dict[str, str]] = None
    capabilities: list[str] = Field(default_factory=lambda: ["exec.readonly"])
    status: str  # online|offline|revoked
    last_seen_at: Optional[datetime] = None
    runner_metadata: Optional[dict[str, Any]] = None
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


class RunnerJobResponse(BaseModel):
    """Response model for a runner job."""

    model_config = ConfigDict(from_attributes=True)

    id: str  # UUID
    owner_id: int
    worker_id: Optional[str] = None
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
# Generic Success Response
# ---------------------------------------------------------------------------


class RunnerSuccessResponse(BaseModel):
    """Generic success response for runner operations."""

    success: bool = True
    message: Optional[str] = None
