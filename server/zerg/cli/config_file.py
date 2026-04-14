"""Config file support for Longhouse runtime configuration.

Loads and manages ``~/.longhouse/config.toml``.

Example config.toml:
    [server]
    host = "127.0.0.1"
    port = 8080

    [shipper]
    flush_ms = 500
    fallback_scan_secs = 300

Precedence: file config < env vars < CLI args
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_runtime_config_path

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
    public_url: str | None = None


@dataclass
class ShipperConfig:
    """Engine (longhouse-engine) configuration."""

    flush_ms: int = 500
    fallback_scan_secs: int = 300


@dataclass
class LonghouseConfig:
    """Complete Longhouse configuration from file."""

    server: ServerConfig = field(default_factory=ServerConfig)
    shipper: ShipperConfig = field(default_factory=ShipperConfig)

    # Track where each setting came from
    _sources: dict[str, str] = field(default_factory=dict)


def get_config_path() -> Path:
    """Get the path to the config file."""
    return get_runtime_config_path()


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
                if "public_url" in server_data:
                    config.server.public_url = server_data["public_url"]
                    sources["server.public_url"] = "file"

            # Load shipper config
            if "shipper" in data:
                shipper_data = data["shipper"]
                if "flush_ms" in shipper_data:
                    config.shipper.flush_ms = int(shipper_data["flush_ms"])
                    sources["shipper.flush_ms"] = "file"
                if "fallback_scan_secs" in shipper_data:
                    config.shipper.fallback_scan_secs = int(shipper_data["fallback_scan_secs"])
                    sources["shipper.fallback_scan_secs"] = "file"

        except Exception as e:
            # Log but don't fail on config file errors
            import logging

            logging.getLogger(__name__).warning(f"Failed to load config file: {e}")

    # Override with environment variables
    if os.getenv("LONGHOUSE_PUBLIC_URL"):
        config.server.public_url = os.environ["LONGHOUSE_PUBLIC_URL"]
        sources["server.public_url"] = "env"
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
    if os.getenv("LONGHOUSE_FLUSH_MS"):
        try:
            config.shipper.flush_ms = int(os.environ["LONGHOUSE_FLUSH_MS"])
            sources["shipper.flush_ms"] = "env"
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(f"Invalid LONGHOUSE_FLUSH_MS value: {os.environ['LONGHOUSE_FLUSH_MS']!r}, ignoring")

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
            if value is None:
                continue
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
        ("server.public_url", config.server.public_url or "(not set)", config._sources.get("server.public_url", "default")),
        ("shipper.flush_ms", str(config.shipper.flush_ms), config._sources.get("shipper.flush_ms", "default")),
        (
            "shipper.fallback_scan_secs",
            str(config.shipper.fallback_scan_secs),
            config._sources.get("shipper.fallback_scan_secs", "default"),
        ),
    ]
    return entries
