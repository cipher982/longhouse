"""Pydantic schemas for agent connector credentials API.

These schemas define the request/response models for the agent connectors
API endpoints used to configure credentials for built-in connector tools.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Credential Field Definition (used in connector metadata)
# ---------------------------------------------------------------------------


class CredentialFieldSchema(BaseModel):
    """Schema for a single credential field definition."""

    key: str = Field(..., description="Field key used in storage")
    label: str = Field(..., description="Human-readable label")
    type: str = Field(..., description="Input type: 'text', 'password', 'url'")
    placeholder: str = Field(..., description="Placeholder text")
    required: bool = Field(..., description="Whether the field is required")


# ---------------------------------------------------------------------------
# Connector Status (list response)
# ---------------------------------------------------------------------------


class ConnectorStatusResponse(BaseModel):
    """Status of a connector type for an agent.

    Used in the list endpoint to show all available connectors
    and their configuration status.
    """

    type: str = Field(..., description="Connector type identifier (e.g., 'slack', 'github')")
    name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="Short description of the connector")
    category: str = Field(..., description="Category: 'notifications' or 'project_management'")
    icon: str = Field(..., description="Icon identifier for UI")
    docs_url: str = Field(..., description="URL to setup documentation")
    fields: list[CredentialFieldSchema] = Field(..., description="Required credential fields")
    configured: bool = Field(..., description="Whether credentials are configured")
    display_name: Optional[str] = Field(None, description="User-provided display name")
    test_status: str = Field("untested", description="Test status: 'untested', 'success', 'failed'")
    last_tested_at: Optional[datetime] = Field(None, description="When credentials were last tested")
    metadata: Optional[dict[str, Any]] = Field(None, description="Metadata from last successful test")


# ---------------------------------------------------------------------------
# Configure Connector Request
# ---------------------------------------------------------------------------


class ConnectorConfigureRequest(BaseModel):
    """Request to configure (create or update) connector credentials."""

    connector_type: str = Field(..., description="Connector type to configure (e.g., 'slack', 'github')")
    credentials: dict[str, str] = Field(
        ...,
        description="Credential values keyed by field name (e.g., {'webhook_url': 'https://...'})",
    )
    display_name: Optional[str] = Field(None, description="Optional user-friendly label")


# ---------------------------------------------------------------------------
# Test Connector Response
# ---------------------------------------------------------------------------


class ConnectorTestResponse(BaseModel):
    """Response from testing a connector's credentials."""

    success: bool = Field(..., description="Whether the test succeeded")
    message: str = Field(..., description="Human-readable result message")
    metadata: Optional[dict[str, Any]] = Field(None, description="Discovered metadata (e.g., username, scopes)")


# ---------------------------------------------------------------------------
# Test Connector Request (for testing before saving)
# ---------------------------------------------------------------------------


class ConnectorTestRequest(BaseModel):
    """Request to test credentials before saving."""

    connector_type: str = Field(..., description="Connector type to test")
    credentials: dict[str, str] = Field(..., description="Credential values to test")


# ---------------------------------------------------------------------------
# Generic Success Response
# ---------------------------------------------------------------------------


class ConnectorSuccessResponse(BaseModel):
    """Generic success response for configure/delete operations."""

    success: bool = True


# ---------------------------------------------------------------------------
# Connector Detail Response (single connector)
# ---------------------------------------------------------------------------


class ConnectorDetailResponse(BaseModel):
    """Detailed response for a single configured connector.

    Note: Credentials are never returned - only metadata.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    connector_type: str
    display_name: Optional[str] = None
    test_status: str = "untested"
    last_tested_at: Optional[datetime] = None
    metadata: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
