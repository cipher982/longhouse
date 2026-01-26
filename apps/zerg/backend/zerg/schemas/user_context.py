"""Pydantic schemas for user context validation.

User context is stored as JSONB and used for prompt composition. These schemas
provide validation while maintaining backwards compatibility through extra="allow".
"""

from typing import Any
from typing import Dict
from typing import Optional

from pydantic import BaseModel
from pydantic import Field


class ServerConfig(BaseModel):
    """Configuration for a single server in user context.

    Examples:
        {"name": "prod-web", "ip": "192.0.2.10", "purpose": "Production VPS"}
        {"name": "gpu-server", "platform": "Ubuntu", "notes": "GPU compute server"}
    """

    name: str = Field(..., description="Server name or hostname")
    ip: Optional[str] = Field(None, description="IP address (WAN or Tailscale)")
    purpose: Optional[str] = Field(None, description="Server purpose or role")
    platform: Optional[str] = Field(None, description="OS/platform (e.g., 'Ubuntu', 'macOS')")
    notes: Optional[str] = Field(None, description="Additional notes or details")
    # SSH configuration for commis fallback
    ssh_alias: Optional[str] = Field(None, description="SSH config alias (e.g., 'cube')")
    ssh_user: Optional[str] = Field(None, description="SSH username (e.g., 'drose')")
    ssh_host: Optional[str] = Field(None, description="SSH hostname/IP (defaults to ip if not set)")
    ssh_port: Optional[int] = Field(None, description="SSH port (defaults to 22 if not set)")

    class Config:
        extra = "allow"  # Allow additional fields for flexibility


class ToolsConfig(BaseModel):
    """Configuration for enabled/disabled tools and integrations.

    Controls which tools are available to fiches when operating on behalf
    of this user.
    """

    location: bool = Field(True, description="Enable location-based features")
    whoop: bool = Field(True, description="Enable Whoop fitness integration")
    obsidian: bool = Field(True, description="Enable Obsidian vault access")
    concierge: bool = Field(True, description="Enable concierge fiche delegation")

    class Config:
        extra = "allow"  # Allow additional tools to be added


class UserContext(BaseModel):
    """User context schema for prompt composition and fiche behavior.

    This schema validates the structure of user.context JSONB field while
    maintaining flexibility through extra="allow". Additional fields beyond
    those defined here are preserved for forwards/backwards compatibility.

    Examples:
        {
            "display_name": "Jane",
            "role": "Software Engineer",
            "location": "San Francisco",
            "servers": [
                {"name": "prod-web", "ip": "192.0.2.10", "purpose": "Production VPS"}
            ],
            "integrations": {
                "github": "janedoe",
                "email": "jane@example.com"
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
    servers: list[ServerConfig] = Field(default_factory=list, description="List of servers user has access to")
    integrations: Dict[str, str] = Field(default_factory=dict, description="Integration credentials or handles")
    tools: ToolsConfig = Field(default_factory=ToolsConfig, description="Tool enablement configuration")
    custom_instructions: Optional[str] = Field(None, description="Custom instructions for fiche behavior")

    class Config:
        extra = "allow"  # Allow additional fields for flexibility
        json_schema_extra = {
            "example": {
                "display_name": "Jane",
                "role": "Software Engineer",
                "location": "San Francisco",
                "servers": [
                    {
                        "name": "prod-web",
                        "ip": "192.0.2.10",
                        "purpose": "Production VPS",
                        "platform": "Ubuntu",
                    }
                ],
                "integrations": {"github": "janedoe", "email": "jane@example.com"},
                "tools": {"location": True, "whoop": True, "obsidian": True, "concierge": True},
                "custom_instructions": "Prefer TypeScript over JavaScript",
            }
        }

    def model_dump(self, **kwargs: Any) -> Dict[str, Any]:
        """Override to ensure we include extra fields in serialization."""
        return super().model_dump(**kwargs)
