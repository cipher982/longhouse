"""Pydantic schemas for Bootstrap API.

These schemas define the request/response models for the admin bootstrap API
endpoints used to seed configuration via API instead of file mounts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import Field

# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


class RunnerSeedItem(BaseModel):
    """Single runner configuration for seeding."""

    name: str = Field(..., description="Runner name")
    secret: str = Field(..., description="Plaintext secret (will be hashed)")
    labels: dict[str, str] | None = Field(None, description="Optional labels")
    capabilities: list[str] = Field(
        default_factory=lambda: ["exec.readonly"],
        description="Runner capabilities",
    )


class RunnersSeedRequest(BaseModel):
    """Request to seed runners for the admin user.

    This replaces file-based seeding from ~/.config/zerg/runners.json.
    """

    runners: list[RunnerSeedItem] = Field(..., description="List of runners to seed")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class CredentialsSeedRequest(BaseModel):
    """Request to seed connector credentials for the admin user.

    Each top-level key is a connector type whose value is that connector's
    credential object; all values are Fernet-encrypted before storage.
    """

    class Config:
        extra = "allow"  # Connector types are open-ended


# ---------------------------------------------------------------------------
# Status Response
# ---------------------------------------------------------------------------


class BootstrapStatusItem(BaseModel):
    """Status of a single bootstrap category."""

    configured: bool = Field(..., description="Whether this category is configured")
    details: str | None = Field(None, description="Additional details")


class BootstrapStatusResponse(BaseModel):
    """Response showing what's configured vs missing."""

    runners: BootstrapStatusItem = Field(..., description="Runners status")
    credentials: BootstrapStatusItem = Field(..., description="Personal credentials status")


# ---------------------------------------------------------------------------
# Generic Response
# ---------------------------------------------------------------------------


class BootstrapSuccessResponse(BaseModel):
    """Generic success response for bootstrap operations."""

    success: bool = True
    message: str = Field(..., description="Operation result message")
    details: dict[str, Any] | None = Field(None, description="Additional details")
