"""Service installation for the Longhouse engine daemon.

Provides cross-platform service management for running longhouse-engine
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


# Service identifiers — kept stable so --uninstall works on existing installs
LAUNCHD_LABEL = "com.longhouse.shipper"
SYSTEMD_UNIT = "longhouse-shipper"

# Legacy service names that were installed directly (before longhouse connect
# --install managed the engine). Detected and superseded on install.
_LEGACY_ENGINE_PLIST_NAME = "com.longhouse.engine.plist"
_LEGACY_SYSTEMD_UNIT_NAME = "longhouse-engine.service"


def _get_legacy_engine_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / _LEGACY_ENGINE_PLIST_NAME


def _get_legacy_systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _LEGACY_SYSTEMD_UNIT_NAME


@dataclass
class ServiceConfig:
    """Configuration for the installed engine service."""

    url: str
    token: str | None = None
    claude_dir: str | None = None
    flush_ms: int = 500
    fallback_scan_secs: int = 300
    spool_replay_secs: int = 30
    log_dir: str | None = None
    compression: str = "zstd"


def detect_platform() -> Platform:
    """Detect the current platform."""
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


def get_engine_executable() -> str:
    """Get the absolute path to the longhouse-engine binary.

    Resolution order:
    1. shutil.which("longhouse-engine")  — installed on PATH
    2. ~/.local/bin/longhouse-engine     — pipx / uv tool install
    3. ~/.claude/bin/longhouse-engine    — Longhouse-managed install
    4. Repo dev builds (release then debug)

    Raises:
        RuntimeError: If the binary cannot be found anywhere.
    """
    # 1. PATH
    found = shutil.which("longhouse-engine")
    if found:
        return found

    # 2. ~/.local/bin
    local_bin = Path.home() / ".local" / "bin" / "longhouse-engine"
    if local_bin.exists():
        return str(local_bin)

    # 3. ~/.claude/bin
    claude_bin = Path.home() / ".claude" / "bin" / "longhouse-engine"
    if claude_bin.exists():
        return str(claude_bin)

    # 4. Repo dev builds
    # service.py lives at apps/zerg/backend/zerg/services/shipper/service.py
    # pyproject.toml is at apps/zerg/backend/ → project_root
    # engine is at apps/engine/ → project_root.parent.parent / "engine"
    project_root = _find_project_root()
    if project_root:
        engine_dir = project_root.parent.parent / "engine"
        for profile in ("release", "debug"):
            candidate = engine_dir / "target" / profile / "longhouse-engine"
            if candidate.exists():
                return str(candidate)

    raise RuntimeError(
        "longhouse-engine not found. " "Install it from https://longhouse.ai/install or build apps/engine (cargo build --release)."
    )


def get_zerg_executable() -> str:
    """Get the path to the Longhouse CLI executable (legacy — for non-engine CLI use).

    Prefers the installed ``longhouse`` command, then falls back to
    legacy ``zerg`` if present, and finally uses ``uv run`` in dev.
    """
    longhouse_path = shutil.which("longhouse")
    if longhouse_path:
        return longhouse_path

    zerg_path = shutil.which("zerg")
    if zerg_path:
        return zerg_path

    uv_path = shutil.which("uv")
    if uv_path:
        project_root = _find_project_root()
        if project_root:
            return f"{uv_path} run --project {project_root} longhouse"
        return f"{uv_path} run longhouse"

    return "longhouse"


def _get_launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _get_systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"


def _resolve_log_dir(config: ServiceConfig) -> Path:
    if config.log_dir:
        return Path(config.log_dir)
    return _resolve_claude_dir(config.claude_dir) / "logs"


def _generate_launchd_plist(config: ServiceConfig) -> str:
    """Generate launchd plist calling longhouse-engine connect."""
    engine = get_engine_executable()
    log_dir = _resolve_log_dir(config)
    claude_dir = _resolve_claude_dir(config.claude_dir)

    args = [
        engine,
        "connect",
        "--flush-ms",
        str(config.flush_ms),
        "--fallback-scan-secs",
        str(config.fallback_scan_secs),
        "--spool-replay-secs",
        str(config.spool_replay_secs),
        "--log-dir",
        str(log_dir),
        "--compression",
        config.compression,
    ]

    program_args = "\n".join(f"        <string>{arg}</string>" for arg in args)

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
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDE_CONFIG_DIR</key>
        <string>{claude_dir}</string>
        <key>LONGHOUSE_LOG_DIR</key>
        <string>{log_dir}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/engine.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/engine.stdout.log</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>Nice</key>
    <integer>10</integer>
    <key>LowPriorityIO</key>
    <true/>
</dict>
</plist>
"""


def _generate_systemd_unit(config: ServiceConfig) -> str:
    """Generate systemd unit calling longhouse-engine connect."""
    engine = get_engine_executable()
    log_dir = _resolve_log_dir(config)
    claude_dir = _resolve_claude_dir(config.claude_dir)

    exec_start = (
        f"{engine} connect"
        f" --flush-ms {config.flush_ms}"
        f" --fallback-scan-secs {config.fallback_scan_secs}"
        f" --spool-replay-secs {config.spool_replay_secs}"
        f" --log-dir {log_dir}"
        f" --compression {config.compression}"
    )

    return f"""[Unit]
Description=Longhouse Engine - Session Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
Environment="CLAUDE_CONFIG_DIR={claude_dir}"
Environment="LONGHOUSE_LOG_DIR={log_dir}"

[Install]
WantedBy=default.target
"""


def install_service(
    url: str,
    token: str | None = None,
    claude_dir: str | None = None,
    flush_ms: int = 500,
    fallback_scan_secs: int = 300,
    spool_replay_secs: int = 30,
    log_dir: str | None = None,
    compression: str = "zstd",
    # Legacy params accepted but ignored (kept for backwards compat during transition)
    _poll_mode: bool = False,
    _interval: int = 30,
) -> dict:
    """Install and start longhouse-engine as a system service.

    Args:
        url: Longhouse API URL (persisted to token file before this is called)
        token: API token (persisted to token file before this is called)
        claude_dir: Claude config directory override
        flush_ms: Milliseconds to flush batched events
        fallback_scan_secs: Seconds between fallback directory scans
        spool_replay_secs: Seconds between spool replay attempts
        log_dir: Override for engine log directory

    Returns:
        Dict with success status and message
    """
    platform = detect_platform()
    config = ServiceConfig(
        url=url,
        token=token,
        claude_dir=claude_dir,
        flush_ms=flush_ms,
        fallback_scan_secs=fallback_scan_secs,
        spool_replay_secs=spool_replay_secs,
        log_dir=log_dir,
        compression=compression,
    )

    # Ensure log directory exists
    _resolve_log_dir(config).mkdir(parents=True, exist_ok=True)

    if platform == Platform.MACOS:
        return _install_launchd(config)
    elif platform == Platform.LINUX:
        return _install_systemd(config)
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _install_launchd(config: ServiceConfig) -> dict:
    """Install launchd service on macOS."""
    plist_path = _get_launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Supersede the legacy engine plist (com.longhouse.engine) if present.
    # It was installed directly before longhouse connect --install managed
    # the engine. Unload and remove it so only one engine runs.
    legacy_plist = _get_legacy_engine_plist_path()
    if legacy_plist.exists():
        try:
            subprocess.run(["launchctl", "unload", str(legacy_plist)], capture_output=True, check=False)
            legacy_plist.unlink()
            logger.info(f"Superseded legacy engine plist at {legacy_plist}")
        except Exception as e:
            logger.warning(f"Failed to remove legacy engine plist: {e}")

    if plist_path.exists():
        try:
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True,
                check=False,
            )
        except Exception as e:
            logger.warning(f"Failed to unload existing service: {e}")

    plist_content = _generate_launchd_plist(config)
    plist_path.write_text(plist_content)
    logger.info(f"Created launchd plist at {plist_path}")

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Failed to load launchd service: {error_msg}")

    log_dir = _resolve_log_dir(config)
    return {
        "success": True,
        "platform": "macos",
        "service": LAUNCHD_LABEL,
        "plist_path": str(plist_path),
        "message": f"Engine service installed and started. Logs at {log_dir}/engine.log.*",
    }


def _install_systemd(config: ServiceConfig) -> dict:
    """Install systemd user service on Linux."""
    unit_path = _get_systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    # Supersede the legacy engine unit (longhouse-engine.service) if present.
    legacy_unit = _get_legacy_systemd_unit_path()
    if legacy_unit.exists():
        try:
            subprocess.run(["systemctl", "--user", "stop", "longhouse-engine"], capture_output=True, check=False)
            subprocess.run(["systemctl", "--user", "disable", "longhouse-engine"], capture_output=True, check=False)
            legacy_unit.unlink()
            logger.info(f"Superseded legacy engine unit at {legacy_unit}")
        except Exception as e:
            logger.warning(f"Failed to remove legacy engine unit: {e}")

    subprocess.run(
        ["systemctl", "--user", "stop", SYSTEMD_UNIT],
        capture_output=True,
        check=False,
    )

    unit_content = _generate_systemd_unit(config)
    unit_path.write_text(unit_content)
    logger.info(f"Created systemd unit at {unit_path}")

    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        check=True,
    )

    result = subprocess.run(
        ["systemctl", "--user", "enable", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Failed to enable systemd service: {error_msg}")

    result = subprocess.run(
        ["systemctl", "--user", "start", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Failed to start systemd service: {error_msg}")

    log_dir = _resolve_log_dir(config)
    return {
        "success": True,
        "platform": "linux",
        "service": SYSTEMD_UNIT,
        "unit_path": str(unit_path),
        "message": f"Engine service installed and started. Logs at {log_dir}/engine.log.*",
    }


def uninstall_service() -> dict:
    """Stop and remove the engine service."""
    platform = detect_platform()

    if platform == Platform.MACOS:
        return _uninstall_launchd()
    elif platform == Platform.LINUX:
        return _uninstall_systemd()
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _uninstall_launchd() -> dict:
    plist_path = _get_launchd_plist_path()

    if not plist_path.exists():
        return {"success": True, "platform": "macos", "message": "Service was not installed"}

    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
        text=True,
    )

    try:
        plist_path.unlink()
    except OSError as e:
        raise RuntimeError(f"Failed to remove plist file: {e}")

    return {"success": True, "platform": "macos", "message": "Service stopped and removed"}


def _uninstall_systemd() -> dict:
    unit_path = _get_systemd_unit_path()

    if not unit_path.exists():
        return {"success": True, "platform": "linux", "message": "Service was not installed"}

    subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT], capture_output=True, check=False)
    subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT], capture_output=True, check=False)

    try:
        unit_path.unlink()
    except OSError as e:
        raise RuntimeError(f"Failed to remove unit file: {e}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)

    return {"success": True, "platform": "linux", "message": "Service stopped and removed"}


def get_service_status() -> ServiceStatus:
    """Get the current status of the engine service."""
    platform = detect_platform()

    if platform == Platform.MACOS:
        return _get_launchd_status()
    elif platform == Platform.LINUX:
        return _get_systemd_status()
    else:
        return "not-installed"


def _get_launchd_status() -> ServiceStatus:
    plist_path = _get_launchd_plist_path()

    if not plist_path.exists():
        return "not-installed"

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return "stopped"

    output = result.stdout.lower()
    if "state = running" in output:
        return "running"

    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("pid ="):
            try:
                pid_str = line.split("=")[1].strip()
                if pid_str.isdigit() and int(pid_str) > 0:
                    return "running"
            except (IndexError, ValueError):
                pass

    return "stopped"


def _get_systemd_status() -> ServiceStatus:
    unit_path = _get_systemd_unit_path()

    if not unit_path.exists():
        return "not-installed"

    result = subprocess.run(
        ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
        capture_output=True,
        text=True,
    )

    return "running" if result.stdout.strip() == "active" else "stopped"


def get_service_info() -> dict:
    """Get detailed information about the engine service."""
    platform = detect_platform()
    status = get_service_status()
    log_dir = _resolve_claude_dir(None) / "logs"

    info = {
        "platform": platform.value,
        "status": status,
        "log_path": str(log_dir / "engine.log.*"),
    }

    if platform == Platform.MACOS:
        info["service_file"] = str(_get_launchd_plist_path())
        info["service_name"] = LAUNCHD_LABEL
    elif platform == Platform.LINUX:
        info["service_file"] = str(_get_systemd_unit_path())
        info["service_name"] = SYSTEMD_UNIT

    return info
