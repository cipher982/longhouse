"""Pydantic schemas for Bootstrap API.

These schemas define the request/response models for the admin bootstrap API
endpoints used to seed configuration via API instead of file mounts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import Field

# ---------------------------------------------------------------------------
# User Context
# ---------------------------------------------------------------------------


class ContextSeedRequest(BaseModel):
    """Request to seed user context for the admin user.

    This replaces file-based seeding from ~/.config/zerg/user_context.json.
    The context is stored in the User.context JSONB column.
    """

    servers: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of servers with name, ip, purpose, ssh_user, etc.",
    )
    integrations: dict[str, Any] = Field(
        default_factory=dict,
        description="Integration configs (github, email, etc.)",
    )
    display_name: str | None = Field(None, description="User's preferred display name")
    role: str | None = Field(None, description="User's job role or title")
    location: str | None = Field(None, description="User's primary location")
    custom_instructions: str | None = Field(None, description="Custom instructions for fiche behavior")

    class Config:
        extra = "allow"  # Allow additional fields for flexibility


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


class TraccarCredentials(BaseModel):
    """Traccar GPS tracking credentials."""

    url: str = Field(..., description="Traccar server URL")
    username: str = Field(..., description="Traccar username")
    password: str = Field(..., description="Traccar password")
    device_id: str = Field(..., description="Device ID to query")


class WhoopCredentials(BaseModel):
    """WHOOP health tracker OAuth credentials."""

    client_id: str = Field(..., description="OAuth client ID")
    client_secret: str = Field(..., description="OAuth client secret")
    access_token: str = Field(..., description="OAuth access token")
    refresh_token: str | None = Field(None, description="OAuth refresh token")


class ObsidianCredentials(BaseModel):
    """Obsidian vault access credentials."""

    vault_path: str = Field(..., description="Path to Obsidian vault")
    runner_name: str = Field(..., description="Runner name with vault access")


class CredentialsSeedRequest(BaseModel):
    """Request to seed personal credentials for the admin user.

    This replaces file-based seeding from ~/.config/zerg/personal_credentials.json.
    All credentials are Fernet-encrypted before storage.
    """

    traccar: TraccarCredentials | None = Field(None, description="Traccar GPS credentials")
    whoop: WhoopCredentials | None = Field(None, description="WHOOP health credentials")
    obsidian: ObsidianCredentials | None = Field(None, description="Obsidian vault credentials")

    class Config:
        extra = "allow"  # Allow additional connector types


# ---------------------------------------------------------------------------
# Status Response
# ---------------------------------------------------------------------------


class BootstrapStatusItem(BaseModel):
    """Status of a single bootstrap category."""

    configured: bool = Field(..., description="Whether this category is configured")
    details: str | None = Field(None, description="Additional details")


class BootstrapStatusResponse(BaseModel):
    """Response showing what's configured vs missing."""

    context: BootstrapStatusItem = Field(..., description="User context status")
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
