"""Configuration schema for MCP server integration.

This module defines the configuration structure for MCP servers, supporting
preset-based, custom HTTP, and stdio (subprocess) configurations.
"""

from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional
from typing import TypedDict
from typing import Union


class MCPPresetConfig(TypedDict):
    """Configuration using a preset."""

    type: Literal["preset"]
    preset: str  # Name of the preset (e.g., "github", "linear", "slack")
    auth_token: Optional[str]  # Optional override for preset's auth token
    allowed_tools: Optional[List[str]]  # Optional override for preset's allowed tools


class MCPCustomConfig(TypedDict):
    """Custom HTTP-based MCP server configuration."""

    type: Literal["custom"]
    name: str  # Server name (used as prefix for tools)
    url: str  # Server URL
    auth_token: Optional[str]  # Authentication token
    allowed_tools: Optional[List[str]]  # List of allowed tools (None = all)
    timeout: Optional[float]  # Request timeout in seconds
    max_retries: Optional[int]  # Maximum number of retries


class MCPStdioConfig(TypedDict):
    """Stdio-based MCP server configuration (subprocess transport)."""

    type: Literal["stdio"]
    name: str  # Server name (used as prefix for tools)
    command: str  # Command to spawn the MCP server (shell-parsed)
    env: Optional[Dict[str, str]]  # Additional environment variables
    allowed_tools: Optional[List[str]]  # List of allowed tools (None = all)
    timeout: Optional[float]  # Request timeout in seconds
    max_retries: Optional[int]  # Maximum number of retries


# Union type for all valid MCP configurations
MCPConfig = Union[MCPPresetConfig, MCPCustomConfig, MCPStdioConfig]


def normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a configuration dict to include type field.

    Args:
        config: Raw configuration dictionary

    Returns:
        Normalized configuration with explicit type field

    Raises:
        ValueError: If configuration is invalid
    """
    # If type is already specified, validate it
    if "type" in config:
        if config["type"] not in ("preset", "custom", "stdio"):
            raise ValueError(f"Invalid configuration type: {config['type']}")
        return config

    # Infer type based on presence of fields
    if "preset" in config:
        return {"type": "preset", **config}
    elif "command" in config and "name" in config:
        # Stdio transport: has command and name, no url
        return {"type": "stdio", **config}
    elif "name" in config and "url" in config:
        # HTTP transport: has url and name
        return {"type": "custom", **config}
    else:
        raise ValueError("Configuration must specify a 'preset', 'name' and 'command' (stdio), or 'name' and 'url' (http)")


def validate_mcp_configs(configs: List[Dict[str, Any]]) -> List[MCPConfig]:
    """Validate and normalize a list of MCP configurations.

    Args:
        configs: List of raw configuration dictionaries

    Returns:
        List of validated and normalized configurations

    Raises:
        ValueError: If any configuration is invalid
    """
    validated = []

    for i, config in enumerate(configs):
        try:
            normalized = normalize_config(config)
            validated.append(normalized)
        except ValueError as e:
            raise ValueError(f"Invalid MCP configuration at index {i}: {e}")

    return validated


# Example usage in agent configuration:
EXAMPLE_MCP_CONFIG = {
    "mcp_servers": [
        # Preset configuration
        {"type": "preset", "preset": "github", "auth_token": "ghp_xxxxx"},
        # Custom HTTP configuration
        {
            "type": "custom",
            "name": "custom_api",
            "url": "https://my-company.com/mcp",
            "auth_token": "custom_token",
            "allowed_tools": ["tool1", "tool2"],
            "timeout": 60.0,
            "max_retries": 5,
        },
        # Stdio (subprocess) configuration
        {
            "type": "stdio",
            "name": "local_server",
            "command": "python -m my_mcp_server",
            "env": {"DEBUG": "1"},
            "allowed_tools": ["read_file", "write_file"],
            "timeout": 30.0,
        },
        # Legacy formats (still supported via normalization)
        {"preset": "linear", "auth_token": "lin_xxxxx"},
        {"name": "inferred_stdio", "command": "npx @some/mcp-server"},  # Infers stdio from command
    ]
}
