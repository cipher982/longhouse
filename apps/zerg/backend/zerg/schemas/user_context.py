"""Pydantic schemas for user context validation.

User context is stored as JSONB and used for prompt composition. These schemas
provide validation while maintaining backwards compatibility through extra="allow".
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    """Configuration for a single server in user context.

    Examples:
        {"name": "clifford", "ip": "REDACTED_IP", "purpose": "Production VPS"}
        {"name": "cube", "platform": "Ubuntu", "notes": "Home GPU server"}
    """

    name: str = Field(..., description="Server name or hostname")
    ip: Optional[str] = Field(None, description="IP address (WAN or Tailscale)")
    purpose: Optional[str] = Field(None, description="Server purpose or role")
    platform: Optional[str] = Field(None, description="OS/platform (e.g., 'Ubuntu', 'macOS')")
    notes: Optional[str] = Field(None, description="Additional notes or details")

    class Config:
        extra = "allow"  # Allow additional fields for flexibility


class ToolsConfig(BaseModel):
    """Configuration for enabled/disabled tools and integrations.

    Controls which tools are available to agents when operating on behalf
    of this user.
    """

    location: bool = Field(True, description="Enable location-based features")
    whoop: bool = Field(True, description="Enable Whoop fitness integration")
    obsidian: bool = Field(True, description="Enable Obsidian vault access")
    supervisor: bool = Field(True, description="Enable supervisor agent delegation")

    class Config:
        extra = "allow"  # Allow additional tools to be added


class UserContext(BaseModel):
    """User context schema for prompt composition and agent behavior.

    This schema validates the structure of user.context JSONB field while
    maintaining flexibility through extra="allow". Additional fields beyond
    those defined here are preserved for forwards/backwards compatibility.

    Examples:
        {
            "display_name": "David",
            "role": "Software Engineer",
            "location": "San Francisco",
            "servers": [
                {"name": "clifford", "ip": "REDACTED_IP", "purpose": "Production VPS"}
            ],
            "integrations": {
                "github": "david-rose",
                "email": "hello@drose.io"
            },
            "tools": {
                "location": true,
                "whoop": true,
                "obsidian": true
            },
            "custom_instructions": "Prefer TypeScript over JavaScript"
        }
    """

    display_name: Optional[str] = Field(None, description="User's preferred display name")
    role: Optional[str] = Field(None, description="User's job role or title")
    location: Optional[str] = Field(None, description="User's primary location")
    description: Optional[str] = Field(None, description="General description or bio")
    servers: list[ServerConfig] = Field(
        default_factory=list, description="List of servers user has access to"
    )
    integrations: Dict[str, str] = Field(
        default_factory=dict, description="Integration credentials or handles"
    )
    tools: ToolsConfig = Field(
        default_factory=ToolsConfig, description="Tool enablement configuration"
    )
    custom_instructions: Optional[str] = Field(
        None, description="Custom instructions for agent behavior"
    )

    class Config:
        extra = "allow"  # Allow additional fields for flexibility
        json_schema_extra = {
            "example": {
                "display_name": "David",
                "role": "Software Engineer",
                "location": "San Francisco",
                "servers": [
                    {
                        "name": "clifford",
                        "ip": "REDACTED_IP",
                        "purpose": "Production VPS",
                        "platform": "Ubuntu",
                    }
                ],
                "integrations": {"github": "david-rose", "email": "hello@drose.io"},
                "tools": {"location": True, "whoop": True, "obsidian": True, "supervisor": True},
                "custom_instructions": "Prefer TypeScript over JavaScript",
            }
        }

    def model_dump(self, **kwargs: Any) -> Dict[str, Any]:
        """Override to ensure we include extra fields in serialization."""
        return super().model_dump(**kwargs)
