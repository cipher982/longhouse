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
# Runtime artifacts can exceed tens of MB; keep onboarding tolerant of slower links.
DOWNLOAD_TIMEOUT_SECONDS = 120.0
RELEASE_CHECKSUMS_FILENAME = "local-runtime-checksums.txt"


class RuntimeComponent(str, Enum):
    ENGINE = "engine"
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
    RuntimeComponent.DESKTOP_APP: RuntimeArtifactKind.APP_BUNDLE,
    RuntimeComponent.DESKTOP_WINDOW: RuntimeArtifactKind.EXECUTABLE,
}

APP_BUNDLE_EXECUTABLE_RELATIVE_PATHS: dict[RuntimeComponent, Path] = {
    RuntimeComponent.DESKTOP_APP: Path("Contents") / "MacOS" / "Longhouse",
}

DEFAULT_SOURCE_ENV_VARS: dict[RuntimeComponent, str] = {
    RuntimeComponent.ENGINE: "LONGHOUSE_ENGINE_SOURCE",
    RuntimeComponent.DESKTOP_APP: "LONGHOUSE_DESKTOP_APP_SOURCE",
    RuntimeComponent.DESKTOP_WINDOW: "LONGHOUSE_DESKTOP_WINDOW_SOURCE",
}

RELEASE_ASSET_FILENAMES: dict[RuntimeComponent, dict[str, str]] = {
    RuntimeComponent.ENGINE: {
        "darwin-arm64": "longhouse-engine-darwin-arm64",
        "linux-x64": "longhouse-engine-linux-x64",
        "linux-arm64": "longhouse-engine-linux-arm64",
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


def _local_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _local_applications_dir() -> Path:
    return Path("/Applications")


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


def _copy_local_binary(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        raise RuntimeError(f"Runtime binary source does not exist: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination_path.name}.",
            suffix=".installing",
            dir=destination_path.parent,
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
        shutil.copy2(source_path, tmp_path)
        tmp_path.chmod(tmp_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp_path, destination_path)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


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


def _resolve_source_override(
    component: RuntimeComponent,
    source_override: str | os.PathLike[str] | None,
) -> str:
    candidates = [source_override, os.getenv(DEFAULT_SOURCE_ENV_VARS[component])]
    candidates.extend(os.getenv(env_var) for env_var in LEGACY_SOURCE_ENV_VARS.get(component, ()))
    for candidate in candidates:
        raw = os.fspath(candidate).strip() if candidate is not None else ""
        if raw:
            return raw
    return ""


def resolve_runtime_source_override(
    component: RuntimeComponent,
    *,
    source_override: str | os.PathLike[str] | None = None,
) -> str:
    """Return the configured source override for a runtime component, if any."""

    return _resolve_source_override(component, source_override)


def ensure_runtime_artifact(
    component: RuntimeComponent,
    *,
    source_override: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
) -> InstalledRuntimeArtifact:
    """Ensure a runtime artifact is available locally and return its paths.

    Resolution order:
    1. explicit source override (path or URL)
    2. already-installed canonical artifact in ``~/.local/bin`` or ``/Applications``
    3. existing engine binary already on PATH (engine only)
    4. released GitHub asset for the current Longhouse version
    """

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
    source_override: str | os.PathLike[str] | None = None,
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
