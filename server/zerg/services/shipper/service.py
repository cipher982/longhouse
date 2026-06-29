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
import xml.sax.saxutils as saxutils
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_log_dir
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import resolve_installed_runtime_artifact

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
COMMON_SERVICE_PATH_SUFFIXES = (
    ".local/bin",
    "bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/home/linuxbrew/.linuxbrew/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

# Legacy service names that were installed directly (before longhouse connect
# --install managed the engine). Detected and superseded on install.
_LEGACY_ENGINE_PLIST_NAME = "com.longhouse.engine.plist"
_LEGACY_SYSTEMD_UNIT_NAME = "longhouse-engine.service"


def _get_legacy_engine_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / _LEGACY_ENGINE_PLIST_NAME


def _get_legacy_systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _LEGACY_SYSTEMD_UNIT_NAME


def _common_service_path() -> str:
    home = Path.home()
    parts = [str(home / suffix) if not suffix.startswith("/") else suffix for suffix in COMMON_SERVICE_PATH_SUFFIXES]
    return ":".join(parts)


def _default_archive_repair_mode_for_url(url: str) -> str:
    """Hosted Runtime Hosts keep archive repair operator-controlled by default."""
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        hostname = ""
    hostname = hostname.lower().rstrip(".")
    if hostname == "longhouse.ai" or hostname.endswith(".longhouse.ai"):
        return "paused"
    return "drain"


def _validate_archive_repair_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized in {"paused", "trickle", "drain"}:
        return normalized
    raise ValueError("archive_repair_mode must be one of: paused, trickle, drain")


@dataclass
class ServiceConfig:
    """Configuration for the installed engine service."""

    url: str
    token: str | None = None
    claude_dir: str | None = None
    fallback_scan_secs: int = 300
    spool_replay_secs: int = 30
    archive_repair_mode: str = "drain"
    log_dir: str | None = None
    compression: str = "zstd"
    machine_name: str | None = None
    machine_config_generation: str | None = None
    machine_state_hash: str | None = None
    prevent_sleep: bool = False


def detect_platform() -> Platform:
    """Detect the current platform."""
    if sys.platform == "darwin":
        return Platform.MACOS
    elif sys.platform.startswith("linux"):
        return Platform.LINUX
    else:
        return Platform.UNSUPPORTED


def _resolve_claude_dir(claude_dir: str | None) -> Path:
    """Resolve the Claude config directory for provider-owned integration state."""
    if claude_dir:
        return Path(claude_dir).expanduser()
    env_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
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
    1. Installed runtime artifact (canonical local install)
    2. Repo dev builds (release then debug)
    3. Binary on PATH via runtime artifact lookup. If PATH and repo builds
       both exist, prefer the repo build; PATH is only the last fallback.

    Raises:
        RuntimeError: If the binary cannot be found anywhere.
    """
    path_fallback = None
    artifact = resolve_installed_runtime_artifact(RuntimeComponent.ENGINE)
    if artifact and artifact.source != "path":
        return artifact.launch_path
    path_fallback = artifact

    # 2. Repo dev builds
    project_root = _find_project_root()
    if project_root:
        engine_dir = project_root.parent / "engine"
        for profile in ("release", "debug"):
            candidate = engine_dir / "target" / profile / "longhouse-engine"
            if candidate.exists():
                return str(candidate)

    # 3. PATH fallback.
    if path_fallback:
        return path_fallback.launch_path

    raise RuntimeError(
        "longhouse-engine not found. " "Install it from https://longhouse.ai/install or build engine (cargo build --release)."
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


def _resolve_default_log_dir(claude_dir: str | None = None) -> Path:
    if claude_dir:
        return get_agent_log_dir(resolve_longhouse_home_from_provider_home(claude_dir))
    return get_agent_log_dir()


def _resolve_log_dir_for_state_root(base_dir: str | None = None) -> Path:
    if base_dir:
        return get_agent_log_dir(Path(base_dir).expanduser())
    return get_agent_log_dir()


def _resolve_log_dir(config: ServiceConfig) -> Path:
    if config.log_dir:
        return Path(config.log_dir)
    return _resolve_default_log_dir(config.claude_dir)


def _resolve_agent_db_path(config: ServiceConfig) -> Path:
    if config.claude_dir:
        return get_agent_db_path(resolve_longhouse_home_from_provider_home(config.claude_dir))
    return get_agent_db_path()


def _resolve_legacy_agent_db_path(config: ServiceConfig) -> Path:
    return _resolve_claude_dir(config.claude_dir) / "longhouse-shipper.db"


def _stop_existing_service_for_install(platform: Platform) -> None:
    """Best-effort stop of existing services before mutating shipper state."""
    if platform == Platform.MACOS:
        for plist_path in (_get_launchd_plist_path(), _get_legacy_engine_plist_path()):
            if plist_path.exists():
                subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
        return

    if platform == Platform.LINUX:
        subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT], capture_output=True, check=False)
        subprocess.run(["systemctl", "--user", "stop", "longhouse-engine"], capture_output=True, check=False)


def _migrate_legacy_shipper_state_if_needed(platform: Platform, config: ServiceConfig) -> Path | None:
    """Move legacy shipper DB files from provider-owned state into Longhouse home."""
    target_db_path = _resolve_agent_db_path(config)
    legacy_db_path = _resolve_legacy_agent_db_path(config)

    if target_db_path.exists() or not legacy_db_path.exists():
        return None

    _stop_existing_service_for_install(platform)
    target_db_path.parent.mkdir(parents=True, exist_ok=True)

    for suffix in ("", "-wal", "-shm"):
        legacy_path = Path(f"{legacy_db_path}{suffix}")
        if not legacy_path.exists():
            continue
        destination_path = Path(f"{target_db_path}{suffix}")
        if destination_path.exists():
            continue
        shutil.move(str(legacy_path), str(destination_path))

    logger.info("Migrated legacy shipper state from %s to %s", legacy_db_path, target_db_path)
    return legacy_db_path


def _has_existing_service_install(platform: Platform) -> bool:
    if platform == Platform.MACOS:
        return _get_launchd_plist_path().exists() or _get_legacy_engine_plist_path().exists()
    if platform == Platform.LINUX:
        return _get_systemd_unit_path().exists() or _get_legacy_systemd_unit_path().exists()
    return False


def _assert_reinstall_preserves_shipper_state(platform: Platform, config: ServiceConfig) -> None:
    """Refuse reinstall when an existing service lost its local shipping state."""
    if not _has_existing_service_install(platform):
        return

    db_path = _resolve_agent_db_path(config)
    if db_path.exists():
        return

    raise RuntimeError(
        "Refusing to reinstall the Machine Agent because an existing local install "
        f"is missing its shipper state DB at {db_path}. "
        "Restore that state or intentionally reset local shipping state before rerunning install."
    )


def _generate_launchd_plist(config: ServiceConfig) -> str:
    """Generate launchd plist calling longhouse-engine connect."""
    engine = get_engine_executable()
    log_dir = _resolve_log_dir(config)
    claude_dir = _resolve_claude_dir(config.claude_dir)
    longhouse_home = resolve_longhouse_home_from_provider_home(claude_dir)

    args = [
        engine,
        "connect",
        "--fallback-scan-secs",
        str(config.fallback_scan_secs),
        "--spool-replay-secs",
        str(config.spool_replay_secs),
        "--archive-repair-mode",
        _validate_archive_repair_mode(config.archive_repair_mode),
        "--compression",
        config.compression,
    ]
    if config.machine_name:
        args += ["--machine-name", config.machine_name]
    if config.prevent_sleep:
        args.append("--prevent-sleep")

    program_args = "\n".join(f"        <string>{saxutils.escape(str(arg))}</string>" for arg in args)
    environment = {
        "CLAUDE_CONFIG_DIR": str(claude_dir),
        "LONGHOUSE_HOME": str(longhouse_home),
        "LONGHOUSE_LOG_DIR": str(log_dir),
        "PATH": _common_service_path(),
    }
    if config.machine_config_generation:
        environment["LONGHOUSE_MACHINE_GENERATION"] = config.machine_config_generation
    if config.machine_state_hash:
        environment["LONGHOUSE_MACHINE_STATE_HASH"] = config.machine_state_hash
    environment_xml = "\n".join(
        f"        <key>{saxutils.escape(key)}</key>\n        <string>{saxutils.escape(value)}</string>"
        for key, value in environment.items()
    )

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
{environment_xml}
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
    longhouse_home = resolve_longhouse_home_from_provider_home(claude_dir)

    machine_name_arg = f" --machine-name {config.machine_name}" if config.machine_name else ""
    exec_start = (
        f"{engine} connect"
        f" --fallback-scan-secs {config.fallback_scan_secs}"
        f" --spool-replay-secs {config.spool_replay_secs}"
        f" --archive-repair-mode {_validate_archive_repair_mode(config.archive_repair_mode)}"
        f" --compression {config.compression}"
        f"{machine_name_arg}"
    )
    environment_lines = [
        f'Environment="CLAUDE_CONFIG_DIR={claude_dir}"',
        f'Environment="LONGHOUSE_HOME={longhouse_home}"',
        f'Environment="LONGHOUSE_LOG_DIR={log_dir}"',
        f'Environment="PATH={_common_service_path()}"',
    ]
    if config.machine_config_generation:
        environment_lines.append(f'Environment="LONGHOUSE_MACHINE_GENERATION={config.machine_config_generation}"')
    if config.machine_state_hash:
        environment_lines.append(f'Environment="LONGHOUSE_MACHINE_STATE_HASH={config.machine_state_hash}"')
    environment_block = "\n".join(environment_lines)

    return f"""[Unit]
Description=Longhouse Engine - Session Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
{environment_block}

[Install]
WantedBy=default.target
"""


def install_service(
    url: str,
    token: str | None = None,
    claude_dir: str | None = None,
    fallback_scan_secs: int = 300,
    spool_replay_secs: int = 30,
    archive_repair_mode: str | None = None,
    log_dir: str | None = None,
    compression: str = "zstd",
    machine_name: str | None = None,
    machine_config_generation: str | None = None,
    machine_state_hash: str | None = None,
    prevent_sleep: bool = False,
    # Legacy params accepted but ignored (kept for backwards compat during transition)
    _poll_mode: bool = False,
    _interval: int = 30,
) -> dict:
    """Install and start longhouse-engine as a system service.

    Args:
        url: Longhouse API URL (persisted to token file before this is called)
        token: API token (persisted to token file before this is called)
        claude_dir: Claude config directory override
        fallback_scan_secs: Seconds between reconciliation directory scans
        spool_replay_secs: Seconds between spool replay attempts
        archive_repair_mode: Archive repair posture. Defaults to paused for
            hosted longhouse.ai Runtime Hosts and drain for custom/self-host URLs.
        log_dir: Override for engine log directory

    Returns:
        Dict with success status and message
    """
    platform = detect_platform()
    config = ServiceConfig(
        url=url,
        token=token,
        claude_dir=claude_dir,
        fallback_scan_secs=fallback_scan_secs,
        spool_replay_secs=spool_replay_secs,
        archive_repair_mode=_validate_archive_repair_mode(archive_repair_mode or _default_archive_repair_mode_for_url(url)),
        log_dir=log_dir,
        compression=compression,
        machine_name=machine_name,
        machine_config_generation=machine_config_generation,
        machine_state_hash=machine_state_hash,
        prevent_sleep=prevent_sleep,
    )

    _migrate_legacy_shipper_state_if_needed(platform, config)
    _assert_reinstall_preserves_shipper_state(platform, config)

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


def get_service_info(claude_dir: str | None = None) -> dict:
    """Get detailed information about the engine service."""
    platform = detect_platform()
    status = get_service_status()
    log_dir = _resolve_log_dir_for_state_root(claude_dir)

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
