"""Local-health desktop helpers for the ambient macOS menu bar app."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import xml.sax.saxutils as saxutils
from pathlib import Path
from typing import Literal

from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_binary
from zerg.services.shipper.service import Platform
from zerg.services.shipper.service import detect_platform

LocalHealthUiStatus = Literal["running", "stopped", "not-installed"]

LAUNCHD_LABEL = "com.longhouse.local-health-menubar"
DEFAULT_REFRESH_SECONDS = 10


def build_local_health_command(*, claude_dir: str | None = None) -> str:
    command = [
        sys.executable,
        "-m",
        "zerg.cli.main",
        "local-health",
        "--json",
    ]
    if claude_dir:
        command.extend(["--claude-dir", claude_dir])
    return shlex.join(command)


def default_install_menubar() -> bool:
    raw = os.getenv("LONGHOUSE_INSTALL_MENUBAR")
    if raw:
        return raw.strip().lower() not in {"0", "false", "no"}
    return detect_platform() == Platform.MACOS and not os.getenv("SSH_CONNECTION") and not os.getenv("CI")


def _log_dir(claude_dir: str | None) -> Path:
    if claude_dir:
        return Path(claude_dir).expanduser() / "logs"
    raw = os.getenv("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser() / "logs"
    return Path.home() / ".claude" / "logs"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _generate_launchd_plist(
    *,
    binary_path: str,
    health_command: str,
    refresh_seconds: int,
    ui_url: str | None,
    claude_dir: str | None,
) -> str:
    program_arguments = [
        binary_path,
        "--live",
        "--refresh-seconds",
        str(refresh_seconds),
        "--health-command",
        health_command,
    ]
    if ui_url:
        program_arguments.extend(["--ui-url", ui_url])

    program_args_xml = "\n".join(f"        <string>{saxutils.escape(str(arg))}</string>" for arg in program_arguments)
    log_dir = _log_dir(claude_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "local-health-menubar.stdout.log"
    stderr_path = log_dir / "local-health-menubar.stderr.log"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
</dict>
</plist>
"""


def get_menubar_service_status() -> LocalHealthUiStatus:
    if detect_platform() != Platform.MACOS:
        return "not-installed"

    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        return "not-installed"

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "stopped"

    output = result.stdout.lower()
    if "state = running" in output:
        return "running"
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("pid ="):
            try:
                pid = int(line.split("=")[1].strip())
            except (IndexError, ValueError):
                continue
            if pid > 0:
                return "running"

    return "stopped"


def get_menubar_service_info() -> dict[str, str]:
    log_dir = _log_dir(None)
    return {
        "platform": detect_platform().value,
        "status": get_menubar_service_status(),
        "service_name": LAUNCHD_LABEL,
        "service_file": str(_launchd_plist_path()),
        "log_path": str(log_dir / "local-health-menubar.*.log"),
    }


def install_menubar_service(
    *,
    ui_url: str | None,
    claude_dir: str | None,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    binary_source_override: str | None = None,
) -> dict[str, str]:
    if detect_platform() != Platform.MACOS:
        raise RuntimeError("Ambient local-health menu bar is only supported on macOS")

    installed_binary = ensure_runtime_binary(
        RuntimeComponent.LOCAL_HEALTH_MENUBAR,
        source_override=binary_source_override,
    )
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)

    plist_path.write_text(
        _generate_launchd_plist(
            binary_path=installed_binary.path,
            health_command=build_local_health_command(claude_dir=claude_dir),
            refresh_seconds=refresh_seconds,
            ui_url=ui_url,
            claude_dir=claude_dir,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error_output = (result.stderr or result.stdout or "launchctl load failed").strip()
        raise RuntimeError(f"Failed to install ambient local-health menu bar: {error_output}")

    return {
        "message": "Ambient local-health menu bar installed",
        "service": "launchd",
        "plist_path": str(plist_path),
        "binary_path": installed_binary.path,
        "binary_source": installed_binary.source,
    }


def uninstall_menubar_service() -> dict[str, str]:
    if detect_platform() != Platform.MACOS:
        return {"success": "true", "platform": detect_platform().value, "message": "Ambient local-health menu bar is not supported"}

    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        return {"success": "true", "platform": "macos", "message": "Ambient local-health menu bar not installed"}

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
    plist_path.unlink(missing_ok=True)
    return {"success": "true", "platform": "macos", "message": "Ambient local-health menu bar removed"}
