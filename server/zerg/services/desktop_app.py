"""Desktop app helpers for the ambient macOS Longhouse app."""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
import xml.sax.saxutils as saxutils
from pathlib import Path
from typing import Literal

from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import ensure_runtime_artifact
from zerg.services.runtime_artifacts import resolve_installed_runtime_artifact
from zerg.services.shipper.service import Platform
from zerg.services.shipper.service import detect_platform

DesktopAppStatus = Literal["running", "stopped", "not-installed"]

LAUNCHD_LABEL = "ai.longhouse.app"
LEGACY_LAUNCHD_LABEL = "com.longhouse.local-health-menubar"
LOG_BASENAME = "desktop-app"
LEGACY_LOG_BASENAME = "local-health-menubar"
DEFAULT_REFRESH_SECONDS = 10


def build_snapshot_command(*, claude_dir: str | None = None) -> str:
    return shlex.join(build_snapshot_arguments(claude_dir=claude_dir))


def build_snapshot_arguments(*, claude_dir: str | None = None) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "zerg.cli.main",
        "local-health",
        "--json",
    ]
    if claude_dir:
        command.extend(["--claude-dir", claude_dir])
    return command


build_local_health_command = build_snapshot_command
build_local_health_arguments = build_snapshot_arguments


def default_install_desktop_app() -> bool:
    raw = os.getenv("LONGHOUSE_INSTALL_MENUBAR")
    if raw:
        return raw.strip().lower() not in {"0", "false", "no"}
    return detect_platform() == Platform.MACOS and not os.getenv("SSH_CONNECTION") and not os.getenv("CI")


default_install_menubar = default_install_desktop_app


def _log_dir(claude_dir: str | None) -> Path:
    if claude_dir:
        return Path(claude_dir).expanduser() / "logs"
    raw = os.getenv("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser() / "logs"
    return Path.home() / ".claude" / "logs"


def _service_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _service_candidates() -> tuple[tuple[Path, str, str], ...]:
    return (
        (_service_plist_path(LAUNCHD_LABEL), LAUNCHD_LABEL, LOG_BASENAME),
        (_service_plist_path(LEGACY_LAUNCHD_LABEL), LEGACY_LAUNCHD_LABEL, LEGACY_LOG_BASENAME),
    )


def _selected_service() -> tuple[Path, str, str]:
    for plist_path, label, log_basename in _service_candidates():
        if plist_path.exists():
            return plist_path, label, log_basename
    return _service_plist_path(LAUNCHD_LABEL), LAUNCHD_LABEL, LOG_BASENAME


def _log_glob_from_stdout(stdout_path: str, fallback_basename: str) -> str:
    expanded = Path(stdout_path).expanduser()
    filename = expanded.name
    if filename.endswith(".stdout.log"):
        base = filename.removesuffix(".stdout.log")
    else:
        base = fallback_basename
    return str(expanded.parent / f"{base}.*.log")


def _generate_launchd_plist(
    *,
    launch_path: str,
    health_arguments: list[str],
    refresh_seconds: int,
    ui_url: str | None,
    claude_dir: str | None,
) -> str:
    if not health_arguments:
        raise ValueError("health_arguments must include an executable path")

    program_arguments = [
        launch_path,
        "--live",
        "--refresh-seconds",
        str(refresh_seconds),
        "--health-exec",
        str(health_arguments[0]),
    ]
    for argument in health_arguments[1:]:
        program_arguments.extend(["--health-arg", str(argument)])
    if ui_url:
        program_arguments.extend(["--ui-url", ui_url])

    program_args_xml = "\n".join(f"        <string>{saxutils.escape(str(arg))}</string>" for arg in program_arguments)
    log_dir = _log_dir(claude_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{LOG_BASENAME}.stdout.log"
    stderr_path = log_dir / f"{LOG_BASENAME}.stderr.log"

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


def get_desktop_app_service_status() -> DesktopAppStatus:
    if detect_platform() != Platform.MACOS:
        return "not-installed"

    plist_path, label, _ = _selected_service()
    if not plist_path.exists():
        return "not-installed"

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{label}"],
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


def get_desktop_app_service_info() -> dict[str, str]:
    plist_path, label, log_basename = _selected_service()
    log_dir = _log_dir(None)
    info = {
        "platform": detect_platform().value,
        "status": get_desktop_app_service_status(),
        "service_name": label,
        "service_file": str(plist_path),
        "log_path": str(log_dir / f"{log_basename}.*.log"),
    }
    artifact = resolve_installed_runtime_artifact(RuntimeComponent.DESKTOP_APP)
    if artifact is not None:
        info["artifact_component"] = artifact.component.value
        info["artifact_path"] = artifact.path
        info["launch_path"] = artifact.launch_path
        info["runtime_mode"] = "app-bundle"
        return info

    if plist_path.exists():
        try:
            payload = plistlib.loads(plist_path.read_bytes())
        except Exception:
            payload = None
        if isinstance(payload, dict):
            stdout_path = payload.get("StandardOutPath")
            if stdout_path:
                info["log_path"] = _log_glob_from_stdout(str(stdout_path), log_basename)
        program_arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        if isinstance(program_arguments, list) and program_arguments:
            launch_path = str(program_arguments[0])
            info["launch_path"] = launch_path
            if ".app/Contents/MacOS/" in launch_path:
                info["artifact_path"] = launch_path.split("/Contents/MacOS/", 1)[0]
            else:
                info["artifact_path"] = launch_path
            info["runtime_mode"] = "broken-install"
    return info


def install_desktop_app_service(
    *,
    ui_url: str | None,
    claude_dir: str | None,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    app_source_override: str | None = None,
) -> dict[str, str]:
    if detect_platform() != Platform.MACOS:
        raise RuntimeError("Longhouse desktop app is only supported on macOS")

    installed_app = ensure_runtime_artifact(
        RuntimeComponent.DESKTOP_APP,
        source_override=app_source_override,
    )
    plist_path = _service_plist_path(LAUNCHD_LABEL)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    for candidate_path, _, _ in _service_candidates():
        if candidate_path.exists():
            subprocess.run(["launchctl", "unload", str(candidate_path)], capture_output=True, check=False)
            candidate_path.unlink(missing_ok=True)

    plist_path.write_text(
        _generate_launchd_plist(
            launch_path=installed_app.launch_path,
            health_arguments=build_snapshot_arguments(claude_dir=claude_dir),
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
        raise RuntimeError(f"Failed to install the Longhouse desktop app: {error_output}")

    return {
        "message": "Longhouse desktop app installed",
        "service": "launchd",
        "plist_path": str(plist_path),
        "app_path": installed_app.path,
        "launch_path": installed_app.launch_path,
        "binary_path": installed_app.launch_path,
        "binary_source": installed_app.source,
    }


def uninstall_desktop_app_service() -> dict[str, str]:
    if detect_platform() != Platform.MACOS:
        return {"success": "true", "platform": detect_platform().value, "message": "Longhouse desktop app is not supported"}

    removed_any = False
    for plist_path, _, _ in _service_candidates():
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
            plist_path.unlink(missing_ok=True)
            removed_any = True

    if not removed_any:
        return {"success": "true", "platform": "macos", "message": "Longhouse desktop app not installed"}

    return {"success": "true", "platform": "macos", "message": "Longhouse desktop app removed"}


get_menubar_service_status = get_desktop_app_service_status
get_menubar_service_info = get_desktop_app_service_info
install_menubar_service = install_desktop_app_service
uninstall_menubar_service = uninstall_desktop_app_service
