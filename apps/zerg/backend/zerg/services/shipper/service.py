"""Service installation for shipper daemon.

Provides cross-platform service management for running the shipper
as a background daemon that starts on boot.

Supports:
- macOS: launchd plist in ~/Library/LaunchAgents
- Linux: systemd user unit in ~/.config/systemd/user

Usage:
    from zerg.services.shipper.service import (
        install_service,
        uninstall_service,
        get_service_status,
    )

    # Install and start the service
    install_service(url="https://api.longhouse.ai", token="xxx")

    # Check status
    status = get_service_status()  # "running", "stopped", or "not-installed"

    # Remove the service
    uninstall_service()
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


class Platform(Enum):
    """Supported platforms for service installation."""

    MACOS = "macos"
    LINUX = "linux"
    UNSUPPORTED = "unsupported"


ServiceStatus = Literal["running", "stopped", "not-installed"]


# Service identifiers
LAUNCHD_LABEL = "com.longhouse.shipper"
SYSTEMD_UNIT = "longhouse-shipper"


@dataclass
class ServiceConfig:
    """Configuration for the installed service."""

    url: str
    token: str | None = None
    claude_dir: str | None = None
    poll_mode: bool = False
    interval: int = 30


def detect_platform() -> Platform:
    """Detect the current platform.

    Returns:
        Platform enum value
    """
    if sys.platform == "darwin":
        return Platform.MACOS
    elif sys.platform.startswith("linux"):
        return Platform.LINUX
    else:
        return Platform.UNSUPPORTED


def _resolve_claude_dir(claude_dir: str | None) -> Path:
    """Resolve the Claude config directory for logs/state."""
    if claude_dir:
        return Path(claude_dir)
    env_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".claude"


def _find_project_root() -> Path | None:
    """Locate the project root containing pyproject.toml (dev installs)."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def get_zerg_executable() -> str:
    """Get the path to the zerg CLI executable.

    First tries to find 'zerg' in PATH, then falls back to
    running via 'uv run zerg' if in a development environment.

    Returns:
        Path or command to run zerg
    """
    # Check if 'zerg' is in PATH
    zerg_path = shutil.which("zerg")
    if zerg_path:
        return zerg_path

    # Check if we're in a uv environment
    uv_path = shutil.which("uv")
    if uv_path:
        project_root = _find_project_root()
        if project_root:
            return f"{uv_path} run --project {project_root} zerg"
        return f"{uv_path} run zerg"

    # Fallback: assume zerg is installed
    return "zerg"


def _get_launchd_plist_path() -> Path:
    """Get the path to the launchd plist file."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _get_systemd_unit_path() -> Path:
    """Get the path to the systemd unit file."""
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"


def _generate_launchd_plist(config: ServiceConfig) -> str:
    """Generate launchd plist content for macOS.

    Args:
        config: Service configuration

    Returns:
        Plist XML content
    """
    zerg_cmd = get_zerg_executable()

    # Build command arguments
    args = ["connect", "--url", config.url]
    if config.poll_mode:
        args.extend(["--poll", "--interval", str(config.interval)])
    if config.claude_dir:
        args.extend(["--claude-dir", config.claude_dir])

    # Build ProgramArguments
    if " " in zerg_cmd:
        # uv run zerg case - split into parts
        parts = zerg_cmd.split() + args
    else:
        parts = [zerg_cmd] + args

    program_args = "\n".join(f"        <string>{arg}</string>" for arg in parts)
    log_path = _resolve_claude_dir(config.claude_dir) / "shipper.log"

    # Build environment variables
    env_dict = ""
    if config.token:
        env_dict = f"""    <key>EnvironmentVariables</key>
    <dict>
        <key>AGENTS_API_TOKEN</key>
        <string>{config.token}</string>
    </dict>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
{env_dict}
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def _generate_systemd_unit(config: ServiceConfig) -> str:
    """Generate systemd unit file content for Linux.

    Args:
        config: Service configuration

    Returns:
        Systemd unit file content
    """
    zerg_cmd = get_zerg_executable()

    # Build command arguments
    args = ["connect", "--url", config.url]
    if config.poll_mode:
        args.extend(["--poll", "--interval", str(config.interval)])
    if config.claude_dir:
        args.extend(["--claude-dir", config.claude_dir])

    exec_start = f"{zerg_cmd} {' '.join(args)}"

    # Build environment
    environment = ""
    if config.token:
        environment = f'Environment="AGENTS_API_TOKEN={config.token}"'

    log_path = _resolve_claude_dir(config.claude_dir) / "shipper.log"

    return f"""[Unit]
Description=Longhouse Shipper - Claude Code Session Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
{environment}

# Logging
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=default.target
"""


def install_service(
    url: str,
    token: str | None = None,
    claude_dir: str | None = None,
    poll_mode: bool = False,
    interval: int = 30,
) -> dict:
    """Install and start the shipper as a system service.

    Creates the appropriate service definition for the current platform
    and starts the service.

    Args:
        url: Zerg API URL
        token: API token for authentication
        claude_dir: Claude config directory (optional)
        poll_mode: Use polling instead of file watching
        interval: Polling interval in seconds

    Returns:
        Dict with success status and message

    Raises:
        RuntimeError: If platform is unsupported or installation fails
    """
    platform = detect_platform()
    config = ServiceConfig(
        url=url,
        token=token,
        claude_dir=claude_dir,
        poll_mode=poll_mode,
        interval=interval,
    )

    # Ensure log directory exists
    log_dir = Path.home() / ".claude"
    log_dir.mkdir(parents=True, exist_ok=True)

    if platform == Platform.MACOS:
        return _install_launchd(config)
    elif platform == Platform.LINUX:
        return _install_systemd(config)
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _install_launchd(config: ServiceConfig) -> dict:
    """Install launchd service on macOS."""
    plist_path = _get_launchd_plist_path()

    # Ensure LaunchAgents directory exists
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Stop existing service if running
    if plist_path.exists():
        try:
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True,
                check=False,  # Don't fail if not loaded
            )
        except Exception as e:
            logger.warning(f"Failed to unload existing service: {e}")

    # Write plist
    plist_content = _generate_launchd_plist(config)
    plist_path.write_text(plist_content)
    logger.info(f"Created launchd plist at {plist_path}")

    # Load and start service
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Failed to load launchd service: {error_msg}")

    return {
        "success": True,
        "platform": "macos",
        "service": LAUNCHD_LABEL,
        "plist_path": str(plist_path),
        "message": f"Service installed and started. Logs at {_resolve_claude_dir(config.claude_dir) / 'shipper.log'}",
    }


def _install_systemd(config: ServiceConfig) -> dict:
    """Install systemd user service on Linux."""
    unit_path = _get_systemd_unit_path()

    # Ensure systemd user directory exists
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    # Stop existing service if running
    subprocess.run(
        ["systemctl", "--user", "stop", SYSTEMD_UNIT],
        capture_output=True,
        check=False,  # Don't fail if not running
    )

    # Write unit file
    unit_content = _generate_systemd_unit(config)
    unit_path.write_text(unit_content)
    logger.info(f"Created systemd unit at {unit_path}")

    # Reload systemd daemon
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        check=True,
    )

    # Enable service (auto-start on boot)
    result = subprocess.run(
        ["systemctl", "--user", "enable", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Failed to enable systemd service: {error_msg}")

    # Start service
    result = subprocess.run(
        ["systemctl", "--user", "start", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Failed to start systemd service: {error_msg}")

    return {
        "success": True,
        "platform": "linux",
        "service": SYSTEMD_UNIT,
        "unit_path": str(unit_path),
        "message": f"Service installed and started. Logs at {_resolve_claude_dir(config.claude_dir) / 'shipper.log'}",
    }


def uninstall_service() -> dict:
    """Stop and remove the shipper service.

    Returns:
        Dict with success status and message

    Raises:
        RuntimeError: If platform is unsupported or uninstallation fails
    """
    platform = detect_platform()

    if platform == Platform.MACOS:
        return _uninstall_launchd()
    elif platform == Platform.LINUX:
        return _uninstall_systemd()
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _uninstall_launchd() -> dict:
    """Uninstall launchd service on macOS."""
    plist_path = _get_launchd_plist_path()

    if not plist_path.exists():
        return {
            "success": True,
            "platform": "macos",
            "message": "Service was not installed",
        }

    # Unload service
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
        text=True,
    )

    # Remove plist file
    try:
        plist_path.unlink()
    except OSError as e:
        raise RuntimeError(f"Failed to remove plist file: {e}")

    return {
        "success": True,
        "platform": "macos",
        "message": "Service stopped and removed",
    }


def _uninstall_systemd() -> dict:
    """Uninstall systemd user service on Linux."""
    unit_path = _get_systemd_unit_path()

    if not unit_path.exists():
        return {
            "success": True,
            "platform": "linux",
            "message": "Service was not installed",
        }

    # Stop service
    subprocess.run(
        ["systemctl", "--user", "stop", SYSTEMD_UNIT],
        capture_output=True,
        check=False,
    )

    # Disable service
    subprocess.run(
        ["systemctl", "--user", "disable", SYSTEMD_UNIT],
        capture_output=True,
        check=False,
    )

    # Remove unit file
    try:
        unit_path.unlink()
    except OSError as e:
        raise RuntimeError(f"Failed to remove unit file: {e}")

    # Reload daemon
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        check=False,
    )

    return {
        "success": True,
        "platform": "linux",
        "message": "Service stopped and removed",
    }


def get_service_status() -> ServiceStatus:
    """Get the current status of the shipper service.

    Returns:
        "running", "stopped", or "not-installed"
    """
    platform = detect_platform()

    if platform == Platform.MACOS:
        return _get_launchd_status()
    elif platform == Platform.LINUX:
        return _get_systemd_status()
    else:
        return "not-installed"


def _get_launchd_status() -> ServiceStatus:
    """Get launchd service status on macOS.

    Uses `launchctl print gui/<uid>/<label>` for reliable status detection.
    Falls back to checking if the plist file exists and service is listed.
    """
    plist_path = _get_launchd_plist_path()

    if not plist_path.exists():
        return "not-installed"

    # Get current user's UID for launchctl print command
    import os

    uid = os.getuid()

    # Use launchctl print for more reliable status detection
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # Service not loaded in launchd
        return "stopped"

    # Parse output to check if service is actually running
    # Look for "state = running" or "pid = <number>" in output
    output = result.stdout.lower()

    # Check for running state
    if "state = running" in output:
        return "running"

    # Check for active PID (pid = <number> where number > 0)
    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("pid ="):
            try:
                pid_str = line.split("=")[1].strip()
                if pid_str.isdigit() and int(pid_str) > 0:
                    return "running"
            except (IndexError, ValueError):
                pass

    # Service is loaded but not running
    return "stopped"


def _get_systemd_status() -> ServiceStatus:
    """Get systemd service status on Linux."""
    unit_path = _get_systemd_unit_path()

    if not unit_path.exists():
        return "not-installed"

    # Check if service is running
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
    )

    status = result.stdout.strip()
    if status == "active":
        return "running"
    else:
        return "stopped"


def get_service_info() -> dict:
    """Get detailed information about the service.

    Returns:
        Dict with platform, status, paths, and configuration
    """
    platform = detect_platform()
    status = get_service_status()

    info = {
        "platform": platform.value,
        "status": status,
        "log_path": str(_resolve_claude_dir(None) / "shipper.log"),
    }

    if platform == Platform.MACOS:
        info["service_file"] = str(_get_launchd_plist_path())
        info["service_name"] = LAUNCHD_LABEL
    elif platform == Platform.LINUX:
        info["service_file"] = str(_get_systemd_unit_path())
        info["service_name"] = SYSTEMD_UNIT

    return info
