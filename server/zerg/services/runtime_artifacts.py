"""Install and locate local Longhouse runtime binaries.

This is the artifact layer for local Longhouse runtime components:

- the Rust engine binary
- the macOS local-health menu bar binary
- the optional window-host binary used for debugging

Higher-level installers (`connect --install`, the shell installer, and future
desktop packaging) should delegate binary acquisition here instead of baking in
release-URL logic or local-path heuristics independently.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path

import httpx

RELEASE_REPO = "cipher982/longhouse"
RELEASE_TAG_PREFIX = "v"
DOWNLOAD_TIMEOUT_SECONDS = 30.0


class RuntimeComponent(str, Enum):
    ENGINE = "engine"
    LOCAL_HEALTH_MENUBAR = "local-health-menubar"
    LOCAL_HEALTH_WINDOW = "local-health-window"


CANONICAL_BINARY_NAMES: dict[RuntimeComponent, str] = {
    RuntimeComponent.ENGINE: "longhouse-engine",
    RuntimeComponent.LOCAL_HEALTH_MENUBAR: "longhouse-local-health-menubar",
    RuntimeComponent.LOCAL_HEALTH_WINDOW: "longhouse-local-health-window",
}

DEFAULT_SOURCE_ENV_VARS: dict[RuntimeComponent, str] = {
    RuntimeComponent.ENGINE: "LONGHOUSE_ENGINE_SOURCE",
    RuntimeComponent.LOCAL_HEALTH_MENUBAR: "LONGHOUSE_LOCAL_HEALTH_MENUBAR_SOURCE",
    RuntimeComponent.LOCAL_HEALTH_WINDOW: "LONGHOUSE_LOCAL_HEALTH_WINDOW_SOURCE",
}

RELEASE_ASSET_FILENAMES: dict[RuntimeComponent, dict[str, str]] = {
    RuntimeComponent.ENGINE: {
        "darwin-arm64": "longhouse-engine-darwin-arm64",
        "linux-x64": "longhouse-engine-linux-x64",
        "linux-arm64": "longhouse-engine-linux-arm64",
    },
    RuntimeComponent.LOCAL_HEALTH_MENUBAR: {
        "darwin-arm64": "longhouse-local-health-menubar-darwin-arm64",
    },
    RuntimeComponent.LOCAL_HEALTH_WINDOW: {
        "darwin-arm64": "longhouse-local-health-window-darwin-arm64",
    },
}


@dataclass(frozen=True)
class InstalledRuntimeBinary:
    component: RuntimeComponent
    path: str
    source: str
    installed_now: bool


def _local_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _canonical_destination(component: RuntimeComponent) -> Path:
    return _local_bin_dir() / CANONICAL_BINARY_NAMES[component]


def _platform_target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "darwin-arm64"
        if machine in {"x86_64", "amd64"}:
            return "darwin-x64"
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "linux-x64"
        if machine in {"arm64", "aarch64"}:
            return "linux-arm64"

    raise RuntimeError(f"Unsupported platform for runtime artifact install: {system}/{machine}")


def _current_release_tag() -> str:
    version = metadata.version("longhouse")
    normalized = version[1:] if version.startswith("v") else version
    return f"{RELEASE_TAG_PREFIX}{normalized}"


def _default_release_asset_url(component: RuntimeComponent) -> str:
    target = _platform_target()
    asset_name = RELEASE_ASSET_FILENAMES.get(component, {}).get(target)
    if not asset_name:
        raise RuntimeError(f"No released {component.value} binary for platform target {target}")
    tag = _current_release_tag()
    return f"https://github.com/{RELEASE_REPO}/releases/download/{tag}/{asset_name}"


def _resolve_existing_binary(component: RuntimeComponent) -> Path | None:
    canonical_path = _canonical_destination(component)
    if canonical_path.exists():
        return canonical_path

    if component == RuntimeComponent.ENGINE:
        found = shutil.which(CANONICAL_BINARY_NAMES[component])
        if found:
            return Path(found)

    return None


def _copy_local_binary(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        raise RuntimeError(f"Runtime binary source does not exist: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    destination_path.chmod(destination_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _download_binary(url: str, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_path.with_suffix(destination_path.suffix + ".download")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            with temp_path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
        temp_path.chmod(temp_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        temp_path.replace(destination_path)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure_runtime_binary(
    component: RuntimeComponent,
    *,
    source_override: str | None = None,
    overwrite: bool = False,
) -> InstalledRuntimeBinary:
    """Ensure a runtime binary is available locally and return its path.

    Resolution order:
    1. explicit source override (path or URL)
    2. already-installed canonical binary in ``~/.local/bin``
    3. existing engine binary already on PATH (engine only)
    4. released GitHub asset for the current Longhouse version
    """

    raw_override = (source_override or os.getenv(DEFAULT_SOURCE_ENV_VARS[component]) or "").strip()
    destination_path = _canonical_destination(component)

    if not raw_override and not overwrite:
        existing = _resolve_existing_binary(component)
        if existing is not None:
            source = "managed-local-bin" if existing == destination_path else "path"
            return InstalledRuntimeBinary(
                component=component,
                path=str(existing),
                source=source,
                installed_now=False,
            )

    if raw_override:
        if raw_override.startswith(("http://", "https://")):
            _download_binary(raw_override, destination_path)
            source = raw_override
        else:
            _copy_local_binary(Path(raw_override).expanduser(), destination_path)
            source = str(Path(raw_override).expanduser())
    else:
        release_url = _default_release_asset_url(component)
        _download_binary(release_url, destination_path)
        source = release_url

    return InstalledRuntimeBinary(
        component=component,
        path=str(destination_path),
        source=source,
        installed_now=True,
    )
