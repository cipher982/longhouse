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
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

import httpx

RELEASE_REPO = "cipher982/longhouse"
RELEASE_TAG_PREFIX = "v"
DOWNLOAD_TIMEOUT_SECONDS = 30.0
RELEASE_CHECKSUMS_FILENAME = "local-runtime-checksums.txt"


class RuntimeComponent(str, Enum):
    ENGINE = "engine"
    LOCAL_HEALTH_APP = "local-health-app"
    LOCAL_HEALTH_WINDOW = "local-health-window"


CANONICAL_BINARY_NAMES: dict[RuntimeComponent, str] = {
    RuntimeComponent.ENGINE: "longhouse-engine",
    RuntimeComponent.LOCAL_HEALTH_WINDOW: "longhouse-local-health-window",
}

CANONICAL_APP_BUNDLE_NAMES: dict[RuntimeComponent, str] = {
    RuntimeComponent.LOCAL_HEALTH_APP: "Longhouse.app",
}


class RuntimeArtifactKind(str, Enum):
    EXECUTABLE = "executable"
    APP_BUNDLE = "app-bundle"


ARTIFACT_KINDS: dict[RuntimeComponent, RuntimeArtifactKind] = {
    RuntimeComponent.ENGINE: RuntimeArtifactKind.EXECUTABLE,
    RuntimeComponent.LOCAL_HEALTH_APP: RuntimeArtifactKind.APP_BUNDLE,
    RuntimeComponent.LOCAL_HEALTH_WINDOW: RuntimeArtifactKind.EXECUTABLE,
}

APP_BUNDLE_EXECUTABLE_RELATIVE_PATHS: dict[RuntimeComponent, Path] = {
    RuntimeComponent.LOCAL_HEALTH_APP: Path("Contents") / "MacOS" / "Longhouse",
}

DEFAULT_SOURCE_ENV_VARS: dict[RuntimeComponent, str] = {
    RuntimeComponent.ENGINE: "LONGHOUSE_ENGINE_SOURCE",
    RuntimeComponent.LOCAL_HEALTH_APP: "LONGHOUSE_LOCAL_HEALTH_APP_SOURCE",
    RuntimeComponent.LOCAL_HEALTH_WINDOW: "LONGHOUSE_LOCAL_HEALTH_WINDOW_SOURCE",
}

RELEASE_ASSET_FILENAMES: dict[RuntimeComponent, dict[str, str]] = {
    RuntimeComponent.ENGINE: {
        "darwin-arm64": "longhouse-engine-darwin-arm64",
        "linux-x64": "longhouse-engine-linux-x64",
        "linux-arm64": "longhouse-engine-linux-arm64",
    },
    RuntimeComponent.LOCAL_HEALTH_APP: {
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


def _local_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _local_applications_dir() -> Path:
    return Path.home() / "Applications"


def _canonical_destination(component: RuntimeComponent) -> Path:
    kind = ARTIFACT_KINDS[component]
    if kind == RuntimeArtifactKind.APP_BUNDLE:
        return _local_applications_dir() / CANONICAL_APP_BUNDLE_NAMES[component]
    return _local_bin_dir() / CANONICAL_BINARY_NAMES[component]


def _artifact_launch_path(component: RuntimeComponent, artifact_path: Path) -> Path:
    kind = ARTIFACT_KINDS[component]
    if kind == RuntimeArtifactKind.APP_BUNDLE:
        return artifact_path / APP_BUNDLE_EXECUTABLE_RELATIVE_PATHS[component]
    return artifact_path


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
    release_assets = RELEASE_ASSET_FILENAMES.get(component)
    if not release_assets:
        raise RuntimeError(f"{component.value} is a local-only runtime artifact and has no published release asset")
    asset_name = release_assets.get(target)
    if not asset_name:
        raise RuntimeError(f"No released {component.value} binary for platform target {target}")
    tag = _current_release_tag()
    return f"https://github.com/{RELEASE_REPO}/releases/download/{tag}/{asset_name}"


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
    canonical_path = _canonical_destination(component)
    if canonical_path.exists():
        launch_path = _artifact_launch_path(component, canonical_path)
        if launch_path.exists():
            source = "local-runtime-app" if ARTIFACT_KINDS[component] == RuntimeArtifactKind.APP_BUNDLE else "local-runtime-bin"
            return InstalledRuntimeArtifact(
                component=component,
                path=str(canonical_path),
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


def ensure_runtime_artifact(
    component: RuntimeComponent,
    *,
    source_override: str | None = None,
    overwrite: bool = False,
) -> InstalledRuntimeArtifact:
    """Ensure a runtime artifact is available locally and return its paths.

    Resolution order:
    1. explicit source override (path or URL)
    2. already-installed canonical artifact in ``~/.local/bin`` or ``~/Applications``
    3. existing engine binary already on PATH (engine only)
    4. released GitHub asset for the current Longhouse version
    """

    raw_override = (source_override or os.getenv(DEFAULT_SOURCE_ENV_VARS[component]) or "").strip()
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
        release_url = _default_release_asset_url(component)
        _install_artifact_from_remote_source(component, release_url, destination_path)
        source = release_url

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
