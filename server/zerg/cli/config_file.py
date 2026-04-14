"""Config file support for Longhouse runtime configuration.

Loads and manages ``~/.longhouse/config.toml``.

Example config.toml:
    [server]
    host = "127.0.0.1"
    port = 8080

    [browser]
    default_url = "http://127.0.0.1:8080"

    [shipper]
    api_url = "https://longhouse.example.com"
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
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.shipper.token import normalize_zerg_url

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
class BrowserConfig:
    """Browser/dashboard target configuration."""

    default_url: str | None = None


@dataclass
class ShipperConfig:
    """Engine (longhouse-engine) configuration."""

    api_url: str | None = None
    flush_ms: int = 500
    fallback_scan_secs: int = 300


@dataclass
class LonghouseConfig:
    """Complete Longhouse configuration from file."""

    server: ServerConfig = field(default_factory=ServerConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    shipper: ShipperConfig = field(default_factory=ShipperConfig)

    # Track where each setting came from
    _sources: dict[str, str] = field(default_factory=dict)


def get_config_path(home_dir: Path | None = None, *, claude_dir: Path | None = None) -> Path:
    """Get the path to the config file.

    When ``claude_dir`` is provided, store the config beside that Claude home
    so disposable test homes and explicit ``--claude-dir`` runs stay isolated.
    """
    if claude_dir is not None:
        return resolve_longhouse_home_from_provider_home(Path(claude_dir).expanduser()) / "config.toml"
    if home_dir is not None:
        return Path(home_dir).expanduser() / ".longhouse" / "config.toml"
    return get_runtime_config_path()


def load_config(config_path: Path | None = None, *, claude_dir: Path | None = None) -> LonghouseConfig:
    """Load configuration from TOML file.

    Args:
        config_path: Optional path to config file. Defaults to ~/.longhouse/config.toml

    Returns:
        LonghouseConfig with values from file (or defaults if file doesn't exist)
    """
    if config_path is None:
        config_path = get_config_path(claude_dir=claude_dir)

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

            # Load browser config
            if "browser" in data:
                browser_data = data["browser"]
                if "default_url" in browser_data:
                    config.browser.default_url = browser_data["default_url"]
                    sources["browser.default_url"] = "file"

            # Load shipper config
            if "shipper" in data:
                shipper_data = data["shipper"]
                if "api_url" in shipper_data:
                    config.shipper.api_url = shipper_data["api_url"]
                    sources["shipper.api_url"] = "file"
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
    if os.getenv("LONGHOUSE_API_URL"):
        config.shipper.api_url = os.environ["LONGHOUSE_API_URL"]
        sources["shipper.api_url"] = "env"
    if os.getenv("LONGHOUSE_FLUSH_MS"):
        try:
            config.shipper.flush_ms = int(os.environ["LONGHOUSE_FLUSH_MS"])
            sources["shipper.flush_ms"] = "env"
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(f"Invalid LONGHOUSE_FLUSH_MS value: {os.environ['LONGHOUSE_FLUSH_MS']!r}, ignoring")

    config._sources = sources
    return config


def save_config(config: dict[str, Any], config_path: Path | None = None, *, claude_dir: Path | None = None) -> None:
    """Save configuration to TOML file with secure permissions.

    Args:
        config: Configuration dict to save
        config_path: Optional path to config file. Defaults to ~/.longhouse/config.toml
    """
    if config_path is None:
        config_path = get_config_path(claude_dir=claude_dir)

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

    if "browser" in config:
        browser_entries: list[str] = []
        for key, value in config["browser"].items():
            if value is None:
                continue
            if isinstance(value, str):
                browser_entries.append(f'{key} = "{value}"')
            else:
                browser_entries.append(f"{key} = {value}")
        if browser_entries:
            lines.append("[browser]")
            lines.extend(browser_entries)
            lines.append("")

    if "shipper" in config:
        shipper_entries: list[str] = []
        for key, value in config["shipper"].items():
            if value is None:
                continue
            if isinstance(value, str):
                shipper_entries.append(f'{key} = "{value}"')
            else:
                shipper_entries.append(f"{key} = {value}")
        if shipper_entries:
            lines.append("[shipper]")
            lines.extend(shipper_entries)
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
        ("browser.default_url", config.browser.default_url or "(not set)", config._sources.get("browser.default_url", "default")),
        ("shipper.api_url", config.shipper.api_url or "(not set)", config._sources.get("shipper.api_url", "default")),
        ("shipper.flush_ms", str(config.shipper.flush_ms), config._sources.get("shipper.flush_ms", "default")),
        (
            "shipper.fallback_scan_secs",
            str(config.shipper.fallback_scan_secs),
            config._sources.get("shipper.fallback_scan_secs", "default"),
        ),
    ]
    return entries


def config_to_dict(config: LonghouseConfig) -> dict[str, Any]:
    """Serialize a loaded config object back to the file schema."""
    return {
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "public_url": config.server.public_url,
        },
        "browser": {
            "default_url": config.browser.default_url,
        },
        "shipper": {
            "api_url": config.shipper.api_url,
            "flush_ms": config.shipper.flush_ms,
            "fallback_scan_secs": config.shipper.fallback_scan_secs,
        },
    }


def save_loaded_config(config: LonghouseConfig, config_path: Path | None = None, *, claude_dir: Path | None = None) -> None:
    """Persist a previously loaded config object."""
    save_config(config_to_dict(config), config_path=config_path, claude_dir=claude_dir)


def get_browser_default_url(config_path: Path | None = None, *, claude_dir: Path | None = None) -> str | None:
    """Return the configured browser/dashboard target, if any."""
    config = load_config(config_path=config_path, claude_dir=claude_dir)
    return normalize_zerg_url(config.browser.default_url)
