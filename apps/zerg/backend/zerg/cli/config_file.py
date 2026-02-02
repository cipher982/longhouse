"""Config file support for Longhouse CLI.

Loads and manages ~/.longhouse/config.toml configuration.

Example config.toml:
    [server]
    host = "127.0.0.1"
    port = 8080

    [shipper]
    mode = "watch"  # or "poll"
    api_url = "http://localhost:8080"
    interval = 30

Precedence: file config < env vars < CLI args
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

# tomllib is built-in from Python 3.11+
try:
    import tomllib
except ImportError:
    # Fallback for Python 3.10
    import tomli as tomllib  # type: ignore


@dataclass
class ServerConfig:
    """Server configuration."""

    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class ShipperConfig:
    """Shipper configuration."""

    mode: str = "watch"  # "watch" or "poll"
    api_url: str = "http://localhost:8080"
    interval: int = 30


@dataclass
class LonghouseConfig:
    """Complete Longhouse configuration from file."""

    server: ServerConfig = field(default_factory=ServerConfig)
    shipper: ShipperConfig = field(default_factory=ShipperConfig)

    # Track where each setting came from
    _sources: dict[str, str] = field(default_factory=dict)


def get_config_path() -> Path:
    """Get the path to the config file."""
    return Path.home() / ".longhouse" / "config.toml"


def load_config(config_path: Path | None = None) -> LonghouseConfig:
    """Load configuration from TOML file.

    Args:
        config_path: Optional path to config file. Defaults to ~/.longhouse/config.toml

    Returns:
        LonghouseConfig with values from file (or defaults if file doesn't exist)
    """
    if config_path is None:
        config_path = get_config_path()

    config = LonghouseConfig()
    sources: dict[str, str] = {}

    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)

            # Load server config
            if "server" in data:
                server_data = data["server"]
                if "host" in server_data:
                    config.server.host = server_data["host"]
                    sources["server.host"] = "file"
                if "port" in server_data:
                    config.server.port = int(server_data["port"])
                    sources["server.port"] = "file"

            # Load shipper config
            if "shipper" in data:
                shipper_data = data["shipper"]
                if "mode" in shipper_data:
                    config.shipper.mode = shipper_data["mode"]
                    sources["shipper.mode"] = "file"
                if "api_url" in shipper_data:
                    config.shipper.api_url = shipper_data["api_url"]
                    sources["shipper.api_url"] = "file"
                if "interval" in shipper_data:
                    config.shipper.interval = int(shipper_data["interval"])
                    sources["shipper.interval"] = "file"

        except Exception as e:
            # Log but don't fail on config file errors
            import logging

            logging.getLogger(__name__).warning(f"Failed to load config file: {e}")

    # Override with environment variables
    if os.getenv("LONGHOUSE_HOST"):
        config.server.host = os.environ["LONGHOUSE_HOST"]
        sources["server.host"] = "env"
    if os.getenv("LONGHOUSE_PORT"):
        try:
            config.server.port = int(os.environ["LONGHOUSE_PORT"])
            sources["server.port"] = "env"
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(f"Invalid LONGHOUSE_PORT value: {os.environ['LONGHOUSE_PORT']!r}, ignoring")
    if os.getenv("LONGHOUSE_SHIPPER_MODE"):
        config.shipper.mode = os.environ["LONGHOUSE_SHIPPER_MODE"]
        sources["shipper.mode"] = "env"
    if os.getenv("LONGHOUSE_API_URL") or os.getenv("ZERG_API_URL"):
        config.shipper.api_url = os.getenv("LONGHOUSE_API_URL") or os.environ["ZERG_API_URL"]
        sources["shipper.api_url"] = "env"
    if os.getenv("LONGHOUSE_SHIPPER_INTERVAL"):
        try:
            config.shipper.interval = int(os.environ["LONGHOUSE_SHIPPER_INTERVAL"])
            sources["shipper.interval"] = "env"
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(
                f"Invalid LONGHOUSE_SHIPPER_INTERVAL value: {os.environ['LONGHOUSE_SHIPPER_INTERVAL']!r}, ignoring"
            )

    config._sources = sources
    return config


def save_config(config: dict[str, Any], config_path: Path | None = None) -> None:
    """Save configuration to TOML file with secure permissions.

    Args:
        config: Configuration dict to save
        config_path: Optional path to config file. Defaults to ~/.longhouse/config.toml
    """
    if config_path is None:
        config_path = get_config_path()

    # Ensure directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Build TOML content manually for clean formatting
    lines: list[str] = []

    if "server" in config:
        lines.append("[server]")
        for key, value in config["server"].items():
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            else:
                lines.append(f"{key} = {value}")
        lines.append("")

    if "shipper" in config:
        lines.append("[shipper]")
        for key, value in config["shipper"].items():
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            else:
                lines.append(f"{key} = {value}")
        lines.append("")

    content = "\n".join(lines)

    # Set umask for secure file creation
    old_umask = os.umask(0o077)
    try:
        config_path.write_text(content)
        # Ensure permissions are correct
        config_path.chmod(0o600)
    finally:
        os.umask(old_umask)


def get_effective_config_display(config: LonghouseConfig) -> list[tuple[str, str, str]]:
    """Get a display list of effective config values with sources.

    Returns:
        List of (key, value, source) tuples
    """
    entries = [
        ("server.host", config.server.host, config._sources.get("server.host", "default")),
        ("server.port", str(config.server.port), config._sources.get("server.port", "default")),
        ("shipper.mode", config.shipper.mode, config._sources.get("shipper.mode", "default")),
        ("shipper.api_url", config.shipper.api_url, config._sources.get("shipper.api_url", "default")),
        ("shipper.interval", str(config.shipper.interval), config._sources.get("shipper.interval", "default")),
    ]
    return entries
