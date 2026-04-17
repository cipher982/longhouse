"""Install and locate local Longhouse runtime artifacts.

This is the artifact layer for local Longhouse runtime components:

- the Rust engine binary
- the macOS ambient Longhouse.app bundle
- the optional window-host binary used for debugging from source

Higher-level installers (`connect --install`, the shell installer, and future
desktop packaging) should delegate binary acquisition here instead of baking in
release-URL logic or local-path heuristics independently.
"""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

import httpx

from zerg.services.longhouse_paths import resolve_longhouse_home

RELEASE_REPO = "cipher982/longhouse"
RELEASE_TAG_PREFIX = "v"
# Runtime artifacts can exceed tens of MB; keep onboarding tolerant of slower links.
DOWNLOAD_TIMEOUT_SECONDS = 120.0
RELEASE_CHECKSUMS_FILENAME = "local-runtime-checksums.txt"
MANAGED_CODEX_LAUNCHER_MARKER = "# longhouse-managed-codex-launcher"
MANAGED_CODEX_VERSION_PATTERN = re.compile(r"(\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]+)?)")


class RuntimeComponent(str, Enum):
    ENGINE = "engine"
    MANAGED_CODEX = "managed-codex"
    DESKTOP_APP = "desktop-app"
    DESKTOP_WINDOW = "desktop-window"
    LOCAL_HEALTH_APP = "desktop-app"
    LOCAL_HEALTH_WINDOW = "desktop-window"

    @classmethod
    def _missing_(cls, value: object) -> RuntimeComponent | None:
        legacy_values = {
            "local-health-app": cls.DESKTOP_APP,
            "local-health-window": cls.DESKTOP_WINDOW,
        }
        if isinstance(value, str):
            return legacy_values.get(value)
        return None


CANONICAL_BINARY_NAMES: dict[RuntimeComponent, str] = {
    RuntimeComponent.ENGINE: "longhouse-engine",
    RuntimeComponent.MANAGED_CODEX: "longhouse-codex",
    RuntimeComponent.DESKTOP_WINDOW: "longhouse-desktop-window",
}

CANONICAL_APP_BUNDLE_NAMES: dict[RuntimeComponent, str] = {
    RuntimeComponent.DESKTOP_APP: "Longhouse.app",
}

LEGACY_BINARY_NAMES: dict[RuntimeComponent, tuple[str, ...]] = {
    RuntimeComponent.DESKTOP_WINDOW: ("longhouse-local-health-window",),
}


class RuntimeArtifactKind(str, Enum):
    EXECUTABLE = "executable"
    APP_BUNDLE = "app-bundle"


ARTIFACT_KINDS: dict[RuntimeComponent, RuntimeArtifactKind] = {
    RuntimeComponent.ENGINE: RuntimeArtifactKind.EXECUTABLE,
    RuntimeComponent.MANAGED_CODEX: RuntimeArtifactKind.EXECUTABLE,
    RuntimeComponent.DESKTOP_APP: RuntimeArtifactKind.APP_BUNDLE,
    RuntimeComponent.DESKTOP_WINDOW: RuntimeArtifactKind.EXECUTABLE,
}

APP_BUNDLE_EXECUTABLE_RELATIVE_PATHS: dict[RuntimeComponent, Path] = {
    RuntimeComponent.DESKTOP_APP: Path("Contents") / "MacOS" / "Longhouse",
}

DEFAULT_SOURCE_ENV_VARS: dict[RuntimeComponent, str] = {
    RuntimeComponent.ENGINE: "LONGHOUSE_ENGINE_SOURCE",
    RuntimeComponent.MANAGED_CODEX: "LONGHOUSE_CODEX_SOURCE",
    RuntimeComponent.DESKTOP_APP: "LONGHOUSE_DESKTOP_APP_SOURCE",
    RuntimeComponent.DESKTOP_WINDOW: "LONGHOUSE_DESKTOP_WINDOW_SOURCE",
}

RELEASE_ASSET_FILENAMES: dict[RuntimeComponent, dict[str, str]] = {
    RuntimeComponent.ENGINE: {
        "darwin-arm64": "longhouse-engine-darwin-arm64",
        "linux-x64": "longhouse-engine-linux-x64",
        "linux-arm64": "longhouse-engine-linux-arm64",
    },
    RuntimeComponent.MANAGED_CODEX: {
        "darwin-arm64": "longhouse-codex-darwin-arm64",
        "linux-x64": "longhouse-codex-linux-x64",
        "linux-arm64": "longhouse-codex-linux-arm64",
    },
    RuntimeComponent.DESKTOP_APP: {
        "darwin-arm64": "Longhouse-macos-arm64.zip",
    },
}

LEGACY_SOURCE_ENV_VARS: dict[RuntimeComponent, tuple[str, ...]] = {
    RuntimeComponent.DESKTOP_APP: ("LONGHOUSE_LOCAL_HEALTH_APP_SOURCE",),
    RuntimeComponent.DESKTOP_WINDOW: ("LONGHOUSE_LOCAL_HEALTH_WINDOW_SOURCE",),
}

LEGACY_RELEASE_ASSET_FILENAMES: dict[RuntimeComponent, dict[str, str]] = {
    RuntimeComponent.DESKTOP_APP: {
        "darwin-arm64": "longhouse-local-health-app-darwin-arm64.zip",
    },
}


@dataclass(frozen=True)
class InstalledRuntimeArtifact:
    component: RuntimeComponent
    path: str
    launch_path: str
    source: str
    installed_now: bool
    kind: RuntimeArtifactKind


InstalledRuntimeBinary = InstalledRuntimeArtifact


@dataclass(frozen=True)
class VersionedExecutableLayout:
    install_root: Path
    versions_dir: Path
    current_link: Path
    launcher_path: Path


def _local_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _local_applications_dir() -> Path:
    return Path("/Applications")


def _managed_codex_layout() -> VersionedExecutableLayout:
    install_root = resolve_longhouse_home() / "runtimes" / "codex"
    return VersionedExecutableLayout(
        install_root=install_root,
        versions_dir=install_root / "versions",
        current_link=install_root / "current",
        launcher_path=_local_bin_dir() / CANONICAL_BINARY_NAMES[RuntimeComponent.MANAGED_CODEX],
    )


def _canonical_destination(component: RuntimeComponent) -> Path:
    kind = ARTIFACT_KINDS[component]
    if kind == RuntimeArtifactKind.APP_BUNDLE:
        return _local_applications_dir() / CANONICAL_APP_BUNDLE_NAMES[component]
    return _local_bin_dir() / CANONICAL_BINARY_NAMES[component]


def desktop_app_canonical_bundle_path() -> Path:
    return _canonical_destination(RuntimeComponent.DESKTOP_APP)


def _installed_destination_candidates(component: RuntimeComponent) -> tuple[Path, ...]:
    candidates = [_canonical_destination(component)]

    if ARTIFACT_KINDS[component] == RuntimeArtifactKind.EXECUTABLE:
        for legacy_name in LEGACY_BINARY_NAMES.get(component, ()):
            candidates.append(_local_bin_dir() / legacy_name)

    return tuple(candidates)


def _artifact_launch_path(component: RuntimeComponent, artifact_path: Path) -> Path:
    kind = ARTIFACT_KINDS[component]
    if kind == RuntimeArtifactKind.APP_BUNDLE:
        return artifact_path / APP_BUNDLE_EXECUTABLE_RELATIVE_PATHS[component]
    return artifact_path


def _should_reuse_installed_artifact(component: RuntimeComponent, artifact_path: Path) -> bool:
    if component != RuntimeComponent.DESKTOP_APP:
        return True

    info_plist = artifact_path / "Contents" / "Info.plist"
    if not info_plist.exists():
        return True

    try:
        payload = plistlib.loads(info_plist.read_bytes())
    except Exception:
        return True

    if not isinstance(payload, dict):
        return True

    version_fields = (
        payload.get("CFBundleShortVersionString"),
        payload.get("CFBundleVersion"),
    )
    for raw_version in version_fields:
        version = str(raw_version or "").strip().lower()
        if version.startswith("0.0.0-smoke") or version.startswith("0.0.0-dev"):
            return False

    return True


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


def _release_asset_filenames(component: RuntimeComponent, target: str) -> tuple[str, ...]:
    filenames: list[str] = []
    current = RELEASE_ASSET_FILENAMES.get(component, {}).get(target)
    legacy = LEGACY_RELEASE_ASSET_FILENAMES.get(component, {}).get(target)
    for filename in (current, legacy):
        if filename and filename not in filenames:
            filenames.append(filename)
    return tuple(filenames)


def _default_release_asset_urls(component: RuntimeComponent) -> tuple[str, ...]:
    target = _platform_target()
    asset_names = _release_asset_filenames(component, target)
    if not asset_names:
        raise RuntimeError(f"{component.value} is a local-only runtime artifact and has no published release asset")
    tag = _current_release_tag()
    return tuple(f"https://github.com/{RELEASE_REPO}/releases/download/{tag}/{asset_name}" for asset_name in asset_names)


def _default_release_asset_url(component: RuntimeComponent) -> str:
    return _default_release_asset_urls(component)[0]


def _release_download_info(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != "github.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    repo_parts = RELEASE_REPO.split("/")
    if len(parts) < 6 or parts[:2] != repo_parts or parts[2:4] != ["releases", "download"]:
        return None

    tag = parts[4]
    asset_name = parts[5]
    return tag, asset_name


def _managed_codex_binary_path(version_dir: Path) -> Path:
    return version_dir / CANONICAL_BINARY_NAMES[RuntimeComponent.MANAGED_CODEX]


def _managed_codex_launcher_script(layout: VersionedExecutableLayout) -> str:
    return (
        "#!/bin/sh\n"
        f"{MANAGED_CODEX_LAUNCHER_MARKER}\n"
        "set -eu\n\n"
        f"CURRENT_LINK={shlex.quote(str(layout.current_link))}\n"
        'TARGET="$CURRENT_LINK/longhouse-codex"\n\n'
        'if [ ! -x "$TARGET" ]; then\n'
        '  echo "Managed Codex runtime is not installed under $CURRENT_LINK" >&2\n'
        "  exit 1\n"
        "fi\n\n"
        'exec "$TARGET" -c check_for_update_on_startup=false "$@"\n'
    )


def _is_managed_codex_launcher(path: Path) -> bool:
    try:
        return MANAGED_CODEX_LAUNCHER_MARKER in path.read_text(errors="ignore")
    except OSError:
        return False


def _install_executable_launcher(layout: VersionedExecutableLayout, script: str) -> None:
    layout.launcher_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = layout.launcher_path.parent / f".{layout.launcher_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    temp_path.write_text(script)
    temp_path.chmod(0o755)
    temp_path.replace(layout.launcher_path)


def _switch_current_version(layout: VersionedExecutableLayout, version_dir: Path) -> None:
    if not _managed_codex_binary_path(version_dir).exists():
        raise RuntimeError(f"Managed Codex version is not installed under {version_dir}")
    layout.install_root.mkdir(parents=True, exist_ok=True)
    temp_link = layout.install_root / f".current.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    temp_link.symlink_to(version_dir)
    temp_link.replace(layout.current_link)


def _extract_version_token(raw: str | None) -> str | None:
    if not raw:
        return None
    match = MANAGED_CODEX_VERSION_PATTERN.search(raw)
    if not match:
        return None
    token = re.sub(r"[^A-Za-z0-9._+-]+", "-", match.group(1)).strip("-")
    return token or None


def _probe_executable_version(binary_path: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(binary_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    return output or None


def _managed_codex_version_id(binary_path: Path, source_hint: str, source_hash: str) -> str:
    version_token = _extract_version_token(_probe_executable_version(binary_path)) or _extract_version_token(source_hint) or "unknown"
    return f"{version_token}-{source_hash[:12]}"


def _resolved_managed_codex_versioned_artifact() -> InstalledRuntimeArtifact | None:
    layout = _managed_codex_layout()
    launcher_path = layout.launcher_path
    current_binary = _managed_codex_binary_path(layout.current_link)
    if launcher_path.exists() and current_binary.exists() and _is_managed_codex_launcher(launcher_path):
        return InstalledRuntimeArtifact(
            component=RuntimeComponent.MANAGED_CODEX,
            path=str(launcher_path),
            launch_path=str(launcher_path),
            source="local-runtime-managed",
            installed_now=False,
            kind=RuntimeArtifactKind.EXECUTABLE,
        )
    return None


def _resolved_managed_codex_legacy_artifact() -> InstalledRuntimeArtifact | None:
    legacy_path = _canonical_destination(RuntimeComponent.MANAGED_CODEX)
    if not legacy_path.exists() or not os.access(legacy_path, os.X_OK):
        return None
    if _is_managed_codex_launcher(legacy_path):
        return None
    return InstalledRuntimeArtifact(
        component=RuntimeComponent.MANAGED_CODEX,
        path=str(legacy_path),
        launch_path=str(legacy_path),
        source="local-runtime-bin-legacy",
        installed_now=False,
        kind=RuntimeArtifactKind.EXECUTABLE,
    )


@lru_cache(maxsize=8)
def _load_release_checksums(tag: str) -> dict[str, str]:
    url = f"https://github.com/{RELEASE_REPO}/releases/download/{tag}/{RELEASE_CHECKSUMS_FILENAME}"
    response = httpx.get(url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()

    checksums: dict[str, str] = {}
    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        checksum, filename = parts
        checksums[Path(filename.lstrip("*")).name] = checksum.lower()
    return checksums


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if chunk:
                digest.update(chunk)
    return digest.hexdigest()


def _verify_download_checksum(url: str, downloaded_path: Path) -> None:
    download_info = _release_download_info(url)
    if download_info is None:
        return

    tag, asset_name = download_info
    checksums = _load_release_checksums(tag)
    expected = checksums.get(asset_name)
    if not expected:
        raise RuntimeError(f"Missing checksum for runtime artifact {asset_name} in release {tag}")

    actual = _sha256_file(downloaded_path)
    if actual != expected:
        raise RuntimeError(f"Checksum mismatch for runtime artifact {asset_name} from release {tag}")


def resolve_installed_runtime_artifact(component: RuntimeComponent) -> InstalledRuntimeArtifact | None:
    if component == RuntimeComponent.MANAGED_CODEX:
        managed = _resolved_managed_codex_versioned_artifact()
        if managed is not None:
            return managed
        legacy = _resolved_managed_codex_legacy_artifact()
        if legacy is not None:
            return legacy
        return None

    for installed_path in _installed_destination_candidates(component):
        if not installed_path.exists():
            continue
        launch_path = _artifact_launch_path(component, installed_path)
        if launch_path.exists():
            if not _should_reuse_installed_artifact(component, installed_path):
                continue
            source = "local-runtime-app" if ARTIFACT_KINDS[component] == RuntimeArtifactKind.APP_BUNDLE else "local-runtime-bin"
            return InstalledRuntimeArtifact(
                component=component,
                path=str(installed_path),
                launch_path=str(launch_path),
                source=source,
                installed_now=False,
                kind=ARTIFACT_KINDS[component],
            )

    if component == RuntimeComponent.ENGINE:
        found = shutil.which(CANONICAL_BINARY_NAMES[component])
        if found:
            found_path = Path(found)
            return InstalledRuntimeArtifact(
                component=component,
                path=str(found_path),
                launch_path=str(found_path),
                source="path",
                installed_now=False,
                kind=RuntimeArtifactKind.EXECUTABLE,
            )

    return None


def _ensure_managed_codex_versioned_artifact_from_binary(
    source_binary: Path,
    *,
    source_label: str,
    overwrite: bool,
) -> InstalledRuntimeArtifact:
    layout = _managed_codex_layout()
    layout.versions_dir.mkdir(parents=True, exist_ok=True)
    layout.launcher_path.parent.mkdir(parents=True, exist_ok=True)

    source_hash = _sha256_file(source_binary)
    version_id = _managed_codex_version_id(source_binary, source_label, source_hash)
    version_dir = layout.versions_dir / version_id
    version_binary = _managed_codex_binary_path(version_dir)
    version_dir.mkdir(parents=True, exist_ok=True)
    existed_before = version_binary.exists()
    try:
        source_matches_installed = source_binary.exists() and version_binary.exists() and source_binary.samefile(version_binary)
    except OSError:
        source_matches_installed = False

    if (overwrite or not existed_before) and not source_matches_installed:
        _copy_local_binary(source_binary, version_binary)

    try:
        current_target = layout.current_link.resolve(strict=True)
    except OSError:
        current_target = None

    launcher_ready = layout.launcher_path.exists() and _is_managed_codex_launcher(layout.launcher_path)
    needs_switch = current_target != version_dir
    changed = overwrite or not existed_before or needs_switch or not launcher_ready

    if needs_switch:
        _switch_current_version(layout, version_dir)
    if not launcher_ready or needs_switch or overwrite:
        _install_executable_launcher(layout, _managed_codex_launcher_script(layout))

    return InstalledRuntimeArtifact(
        component=RuntimeComponent.MANAGED_CODEX,
        path=str(layout.launcher_path),
        launch_path=str(layout.launcher_path),
        source=source_label,
        installed_now=changed,
        kind=RuntimeArtifactKind.EXECUTABLE,
    )


def _ensure_managed_codex_runtime_artifact(
    *,
    source_override: str | None = None,
    overwrite: bool = False,
) -> InstalledRuntimeArtifact:
    raw_override = _resolve_source_override(RuntimeComponent.MANAGED_CODEX, source_override)

    if not raw_override and not overwrite:
        existing = _resolved_managed_codex_versioned_artifact()
        if existing is not None:
            return existing

    install_source = raw_override
    if not install_source:
        legacy = _resolved_managed_codex_legacy_artifact()
        if legacy is not None:
            install_source = legacy.path
        else:
            install_source = _default_release_asset_url(RuntimeComponent.MANAGED_CODEX)

    if install_source.startswith(("http://", "https://")):
        temp_path = _download_to_temp_path(install_source)
        try:
            _verify_download_checksum(install_source, temp_path)
            return _ensure_managed_codex_versioned_artifact_from_binary(
                temp_path,
                source_label=install_source,
                overwrite=overwrite,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    source_path = Path(install_source).expanduser()
    layout = _managed_codex_layout()
    if source_path == layout.launcher_path and _is_managed_codex_launcher(source_path):
        source_path = _managed_codex_binary_path(layout.current_link)
    return _ensure_managed_codex_versioned_artifact_from_binary(
        source_path,
        source_label=str(source_path),
        overwrite=overwrite,
    )


def _copy_local_binary(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        raise RuntimeError(f"Runtime binary source does not exist: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    destination_path.chmod(destination_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _copy_local_app_bundle(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        raise RuntimeError(f"Runtime app bundle source does not exist: {source_path}")
    if not source_path.is_dir() or source_path.suffix != ".app":
        raise RuntimeError(f"Runtime app bundle source must be a .app directory: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_path.parent / f".{destination_path.name}.installing"
    try:
        shutil.rmtree(temp_path, ignore_errors=True)
        shutil.copytree(source_path, temp_path)
        shutil.rmtree(destination_path, ignore_errors=True)
        temp_path.replace(destination_path)
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def _extract_app_bundle_archive(source_path: Path, destination_path: Path) -> None:
    if source_path.suffix != ".zip":
        raise RuntimeError(f"Unsupported app bundle archive format: {source_path}")

    extract_root = destination_path.parent / f".{destination_path.name}.extract"
    try:
        shutil.rmtree(extract_root, ignore_errors=True)
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source_path) as archive:
            archive.extractall(extract_root)
        app_candidates = [path for path in extract_root.rglob("*.app") if path.is_dir()]
        if len(app_candidates) != 1:
            raise RuntimeError(f"Expected exactly one .app bundle in runtime archive {source_path}, found {len(app_candidates)}")
        _copy_local_app_bundle(app_candidates[0], destination_path)
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)


def _install_artifact_from_local_source(
    component: RuntimeComponent,
    source_path: Path,
    destination_path: Path,
) -> None:
    kind = ARTIFACT_KINDS[component]
    expanded_source = source_path.expanduser()
    if kind == RuntimeArtifactKind.APP_BUNDLE:
        if expanded_source.is_dir():
            _copy_local_app_bundle(expanded_source, destination_path)
            return
        if expanded_source.is_file():
            _extract_app_bundle_archive(expanded_source, destination_path)
            return
        raise RuntimeError(f"Runtime artifact source does not exist: {expanded_source}")

    _copy_local_binary(expanded_source, destination_path)


def _download_to_temp_path(url: str) -> Path:
    parsed = Path(urlparse(url).path)
    suffix = "".join(parsed.suffixes)
    fd, raw_path = tempfile.mkstemp(prefix="longhouse-runtime-", suffix=suffix)
    os.close(fd)
    temp_path = Path(raw_path)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            with temp_path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _install_artifact_from_remote_source(
    component: RuntimeComponent,
    url: str,
    destination_path: Path,
) -> None:
    temp_path = _download_to_temp_path(url)
    try:
        _verify_download_checksum(url, temp_path)
        _install_artifact_from_local_source(component, temp_path, destination_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _resolve_source_override(component: RuntimeComponent, source_override: str | None) -> str:
    candidates = [source_override, os.getenv(DEFAULT_SOURCE_ENV_VARS[component])]
    candidates.extend(os.getenv(env_var) for env_var in LEGACY_SOURCE_ENV_VARS.get(component, ()))
    for candidate in candidates:
        raw = (candidate or "").strip()
        if raw:
            return raw
    return ""


def resolve_runtime_source_override(
    component: RuntimeComponent,
    *,
    source_override: str | None = None,
) -> str:
    """Return the configured source override for a runtime component, if any."""

    return _resolve_source_override(component, source_override)


def ensure_runtime_artifact(
    component: RuntimeComponent,
    *,
    source_override: str | None = None,
    overwrite: bool = False,
) -> InstalledRuntimeArtifact:
    """Ensure a runtime artifact is available locally and return its paths.

    Resolution order:
    1. explicit source override (path or URL)
    2. already-installed canonical artifact in ``~/.local/bin`` or ``/Applications``
    3. existing engine binary already on PATH (engine only)
    4. released GitHub asset for the current Longhouse version
    """

    if component == RuntimeComponent.MANAGED_CODEX:
        return _ensure_managed_codex_runtime_artifact(
            source_override=source_override,
            overwrite=overwrite,
        )

    raw_override = _resolve_source_override(component, source_override)
    destination_path = _canonical_destination(component)

    if not raw_override and not overwrite:
        existing = resolve_installed_runtime_artifact(component)
        if existing is not None:
            return existing

    if raw_override:
        if raw_override.startswith(("http://", "https://")):
            _install_artifact_from_remote_source(component, raw_override, destination_path)
            source = raw_override
        else:
            _install_artifact_from_local_source(component, Path(raw_override), destination_path)
            source = str(Path(raw_override).expanduser())
    else:
        source = None
        last_http_error: httpx.HTTPError | None = None
        for release_url in _default_release_asset_urls(component):
            try:
                _install_artifact_from_remote_source(component, release_url, destination_path)
                source = release_url
                break
            except httpx.HTTPError as exc:
                last_http_error = exc
        if source is None:
            if last_http_error is not None:
                raise RuntimeError(
                    f"No released {component.value} artifact is available for platform target {_platform_target()}"
                ) from last_http_error
            raise RuntimeError(f"Unable to install released runtime artifact for {component.value}")

    return InstalledRuntimeArtifact(
        component=component,
        path=str(destination_path),
        launch_path=str(_artifact_launch_path(component, destination_path)),
        source=source,
        installed_now=True,
        kind=ARTIFACT_KINDS[component],
    )


def ensure_runtime_binary(
    component: RuntimeComponent,
    *,
    source_override: str | None = None,
    overwrite: bool = False,
) -> InstalledRuntimeBinary:
    artifact = ensure_runtime_artifact(
        component,
        source_override=source_override,
        overwrite=overwrite,
    )
    if artifact.kind != RuntimeArtifactKind.EXECUTABLE:
        raise RuntimeError(f"{component.value} is packaged as {artifact.kind.value}, not a raw executable")
    return artifact
