"""CLI install metadata and upgrade helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from importlib import metadata
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote
from urllib.parse import urlparse

import httpx
import typer

from zerg.services.longhouse_paths import resolve_longhouse_home

PACKAGE_NAME = "longhouse"
DEFAULT_CHANNEL = "stable"
DEFAULT_INSTALL_METHOD = "uv"
DEFAULT_INSTALL_SOURCE = "pypi"
PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
UPDATE_CACHE_TTL_SECONDS = 24 * 60 * 60
UPDATE_CHECK_LOCK_SECONDS = 5 * 60
UPDATE_CHECK_ENV = "LONGHOUSE_SKIP_UPDATE_NOTIFIER"
UPDATE_CHECK_COMMAND_SNIPPET = "from zerg.cli.update_manager import refresh_update_cache; " "refresh_update_cache(background=True)"


@dataclass(frozen=True)
class InstallMetadata:
    install_method: str
    install_source: str
    package_name: str
    channel: str
    installed_version: str
    installed_at: str
    last_upgrade_at: str
    package_ref: str | None = None


@dataclass(frozen=True)
class UpdateCheckResult:
    installed_version: str
    latest_version: str
    update_available: bool
    install_method: str
    install_source: str
    upgrade_command: str
    package_name: str


@dataclass(frozen=True)
class CachedUpdateCheck:
    checked_at: str
    installed_version: str
    latest_version: str | None
    update_available: bool
    upgrade_command: str
    install_method: str
    install_source: str
    package_name: str
    error: str | None = None


@dataclass(frozen=True)
class DistributionInstallProbe:
    install_method: str | None = None
    install_source: str | None = None
    package_ref: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _install_metadata_path() -> Path:
    return _get_longhouse_home() / "install.json"


def _update_cache_path() -> Path:
    return _get_longhouse_home() / "update-check.json"


def _update_lock_path() -> Path:
    return _get_longhouse_home() / "update-check.lock"


def _get_longhouse_home() -> Path:
    """Return Longhouse home, creating it if needed."""
    longhouse_home = resolve_longhouse_home()
    longhouse_home.mkdir(parents=True, exist_ok=True)
    return longhouse_home


def current_installed_version(package_name: str = PACKAGE_NAME) -> str:
    return metadata.version(package_name)


def _normalize_file_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path = f"//{parsed.netloc}{path}"
    return path or raw_url


def _parse_iso8601(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_install_metadata() -> InstallMetadata | None:
    path = _install_metadata_path()
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return InstallMetadata(
        install_method=str(payload.get("install_method") or DEFAULT_INSTALL_METHOD),
        install_source=str(payload.get("install_source") or "unknown"),
        package_name=str(payload.get("package_name") or PACKAGE_NAME),
        channel=str(payload.get("channel") or DEFAULT_CHANNEL),
        installed_version=str(payload.get("installed_version") or current_installed_version()),
        installed_at=str(payload.get("installed_at") or _utc_now_iso()),
        last_upgrade_at=str(payload.get("last_upgrade_at") or payload.get("installed_at") or _utc_now_iso()),
        package_ref=str(payload.get("package_ref")).strip() if payload.get("package_ref") else None,
    )


def _probe_installed_distribution(package_name: str = PACKAGE_NAME) -> DistributionInstallProbe:
    try:
        distribution = metadata.distribution(package_name)
    except metadata.PackageNotFoundError:
        return DistributionInstallProbe()

    installer = (distribution.read_text("INSTALLER") or "").strip() or None
    direct_url_raw = (distribution.read_text("direct_url.json") or "").strip()
    if not direct_url_raw:
        return DistributionInstallProbe(install_method=installer)

    try:
        direct_url = json.loads(direct_url_raw)
    except json.JSONDecodeError:
        return DistributionInstallProbe(install_method=installer)

    url = str(direct_url.get("url") or "").strip()
    if not url:
        return DistributionInstallProbe(install_method=installer)

    if url.startswith("file://"):
        dir_info = direct_url.get("dir_info") if isinstance(direct_url, dict) else None
        editable = bool(dir_info.get("editable")) if isinstance(dir_info, dict) else False
        return DistributionInstallProbe(
            install_method=installer,
            install_source="editable-path" if editable else "local-path",
            package_ref=_normalize_file_url(url),
        )

    if isinstance(direct_url.get("vcs_info"), dict):
        return DistributionInstallProbe(
            install_method=installer,
            install_source="git",
            package_ref=url,
        )

    return DistributionInstallProbe(
        install_method=installer,
        install_source="custom",
        package_ref=url,
    )


def write_install_metadata(
    *,
    install_method: str,
    install_source: str,
    package_name: str = PACKAGE_NAME,
    channel: str = DEFAULT_CHANNEL,
    package_ref: str | None = None,
) -> InstallMetadata:
    path = _install_metadata_path()
    existing = load_install_metadata()
    now = _utc_now_iso()
    installed_at = existing.installed_at if existing else now
    payload = InstallMetadata(
        install_method=install_method.strip() or DEFAULT_INSTALL_METHOD,
        install_source=install_source.strip() or "unknown",
        package_name=package_name.strip() or PACKAGE_NAME,
        channel=channel.strip() or DEFAULT_CHANNEL,
        installed_version=current_installed_version(package_name.strip() or PACKAGE_NAME),
        installed_at=installed_at,
        last_upgrade_at=now,
        package_ref=(package_ref.strip() if package_ref and package_ref.strip() else None),
    )
    path.write_text(json.dumps(asdict(payload), indent=2) + "\n", encoding="utf-8")
    return payload


def load_update_cache() -> CachedUpdateCheck | None:
    path = _update_cache_path()
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CachedUpdateCheck(
        checked_at=str(payload.get("checked_at") or _utc_now_iso()),
        installed_version=str(payload.get("installed_version") or current_installed_version()),
        latest_version=str(payload.get("latest_version")).strip() if payload.get("latest_version") else None,
        update_available=bool(payload.get("update_available")),
        upgrade_command=str(payload.get("upgrade_command") or recommended_upgrade_command(load_install_metadata())),
        install_method=str(payload.get("install_method") or DEFAULT_INSTALL_METHOD),
        install_source=str(payload.get("install_source") or DEFAULT_INSTALL_SOURCE),
        package_name=str(payload.get("package_name") or PACKAGE_NAME),
        error=str(payload.get("error")).strip() if payload.get("error") else None,
    )


def write_update_cache(result: UpdateCheckResult | None, *, error: str | None = None) -> CachedUpdateCheck:
    install_metadata = detect_install_metadata()
    payload = CachedUpdateCheck(
        checked_at=_utc_now_iso(),
        installed_version=result.installed_version if result else current_installed_version(),
        latest_version=result.latest_version if result else None,
        update_available=result.update_available if result else False,
        upgrade_command=(result.upgrade_command if result else recommended_upgrade_command(install_metadata)),
        install_method=(result.install_method if result else install_metadata.install_method),
        install_source=(result.install_source if result else install_metadata.install_source),
        package_name=(result.package_name if result else install_metadata.package_name),
        error=error.strip() if error else None,
    )
    _update_cache_path().write_text(json.dumps(asdict(payload), indent=2) + "\n", encoding="utf-8")
    return payload


def detect_install_metadata() -> InstallMetadata:
    existing = load_install_metadata()
    probe = _probe_installed_distribution()
    now = _utc_now_iso()
    installed_version = current_installed_version()
    if existing is None:
        return InstallMetadata(
            install_method=probe.install_method or DEFAULT_INSTALL_METHOD,
            install_source=probe.install_source or DEFAULT_INSTALL_SOURCE,
            package_name=PACKAGE_NAME,
            channel=DEFAULT_CHANNEL,
            installed_version=installed_version,
            installed_at=now,
            last_upgrade_at=now,
            package_ref=probe.package_ref,
        )

    install_method = probe.install_method or existing.install_method
    install_source = existing.install_source
    package_ref = existing.package_ref

    if probe.install_source is not None:
        install_source = probe.install_source
        package_ref = probe.package_ref
    elif existing.install_source == "unknown" and probe.install_method == "uv":
        install_source = DEFAULT_INSTALL_SOURCE

    return InstallMetadata(
        install_method=install_method,
        install_source=install_source,
        package_name=existing.package_name,
        channel=existing.channel,
        installed_version=installed_version,
        installed_at=existing.installed_at,
        last_upgrade_at=existing.last_upgrade_at,
        package_ref=package_ref,
    )


def recommended_upgrade_command(metadata_or_none: InstallMetadata | None) -> str:
    metadata = metadata_or_none or detect_install_metadata()
    install_method = metadata.install_method.strip().lower()
    package_name = metadata.package_name.strip() or PACKAGE_NAME
    if install_method == "uv":
        return f"uv tool upgrade {package_name}"
    if install_method == "brew":
        return f"brew upgrade {package_name}"
    if install_method == "npm":
        return f"npm update -g {package_name}"
    return "curl -fsSL https://get.longhouse.ai/install.sh | bash"


def _cache_is_fresh(
    cache_payload: CachedUpdateCheck | None, *, installed_version: str, ttl_seconds: int = UPDATE_CACHE_TTL_SECONDS
) -> bool:
    if cache_payload is None or cache_payload.installed_version != installed_version:
        return False
    checked_at = _parse_iso8601(cache_payload.checked_at)
    if checked_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds()
    return age_seconds < ttl_seconds


def _update_lock_is_fresh(ttl_seconds: int = UPDATE_CHECK_LOCK_SECONDS) -> bool:
    lock_path = _update_lock_path()
    if not lock_path.exists():
        return False
    modified_at = datetime.fromtimestamp(lock_path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - modified_at).total_seconds() < ttl_seconds


def _mark_update_check_started() -> None:
    lock_path = _update_lock_path()
    lock_path.write_text(_utc_now_iso() + "\n", encoding="utf-8")


def _clear_update_check_lock() -> None:
    _update_lock_path().unlink(missing_ok=True)


def fetch_latest_pypi_version(*, package_name: str = PACKAGE_NAME, timeout_secs: float = 2.0) -> str:
    url = PYPI_JSON_URL.format(package=package_name)
    response = httpx.get(
        url,
        timeout=timeout_secs,
        headers={"User-Agent": f"longhouse/{current_installed_version(package_name)}"},
    )
    response.raise_for_status()
    payload = response.json()
    info = payload.get("info") if isinstance(payload, dict) else None
    latest_version = str((info or {}).get("version") or "").strip()
    if not latest_version:
        raise RuntimeError(f"PyPI response for {package_name} missing info.version")
    return latest_version


def _numeric_version_key(raw_version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in raw_version.split("."):
        match = re.match(r"^(\d+)", segment.strip())
        if match is None:
            raise ValueError(f"Version segment {segment!r} is not numeric")
        parts.append(int(match.group(1)))
    return tuple(parts)


def _is_newer_version(latest_version: str, installed_version: str) -> bool:
    try:
        from packaging.version import Version

        return Version(latest_version) > Version(installed_version)
    except ModuleNotFoundError:
        latest_key = _numeric_version_key(latest_version)
        installed_key = _numeric_version_key(installed_version)
        width = max(len(latest_key), len(installed_key))
        return latest_key + (0,) * (width - len(latest_key)) > installed_key + (0,) * (width - len(installed_key))
    except Exception as exc:
        raise RuntimeError(f"Could not compare installed version {installed_version!r} with latest version {latest_version!r}") from exc


def check_for_updates(*, package_name: str = PACKAGE_NAME) -> UpdateCheckResult:
    install_metadata = detect_install_metadata()
    installed_version = current_installed_version(package_name)
    latest_version = fetch_latest_pypi_version(package_name=package_name)
    update_available = _is_newer_version(latest_version, installed_version)
    return UpdateCheckResult(
        installed_version=installed_version,
        latest_version=latest_version,
        update_available=update_available,
        install_method=install_metadata.install_method,
        install_source=install_metadata.install_source,
        upgrade_command=recommended_upgrade_command(install_metadata),
        package_name=package_name,
    )


def refresh_update_cache(*, background: bool = False) -> CachedUpdateCheck:
    if background:
        _mark_update_check_started()
    try:
        result = check_for_updates()
        return write_update_cache(result)
    except Exception as exc:
        return write_update_cache(None, error=str(exc))
    finally:
        if background:
            _clear_update_check_lock()


def spawn_background_update_check() -> bool:
    if _update_lock_is_fresh():
        return False
    _mark_update_check_started()
    env = os.environ.copy()
    env[UPDATE_CHECK_ENV] = "1"
    with open(os.devnull, "wb") as sink:
        subprocess.Popen(
            [sys.executable, "-c", UPDATE_CHECK_COMMAND_SNIPPET],
            stdout=sink,
            stderr=sink,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
    return True


def _should_skip_update_notice(argv: Sequence[str] | None) -> bool:
    args = list(argv or [])
    if os.environ.get(UPDATE_CHECK_ENV) == "1":
        return True
    if "--help" in args or "-h" in args or "--version" in args or "--json" in args:
        return True
    hidden_or_update_commands = {"version", "upgrade", "record-install", "doctor"}
    return any(arg in hidden_or_update_commands for arg in args)


def maybe_notify_update(argv: Sequence[str] | None = None) -> None:
    if _should_skip_update_notice(argv):
        return
    # TTY guard is intentional: non-interactive callers (scripts, pipes, CI)
    # should not get update noise on stderr. Non-TTY users are covered by the
    # menu bar upgrade banner (via local health snapshot) and explicit
    # `longhouse version --check`. Do not remove this without adding an
    # alternative notification path for non-interactive contexts.
    if not sys.stderr.isatty():
        return

    installed_version = current_installed_version()
    cached = load_update_cache()
    if cached and cached.installed_version == installed_version and cached.update_available:
        typer.secho(
            f"Update available: Longhouse {cached.latest_version} (you have {cached.installed_version}). " f"Run: {cached.upgrade_command}",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if _cache_is_fresh(cached, installed_version=installed_version):
        return
    spawn_background_update_check()


def version_command(
    check: bool = typer.Option(
        False,
        "--check",
        help="Check PyPI for the latest stable Longhouse version.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON.",
    ),
) -> None:
    """Show the installed Longhouse version."""

    installed_version = current_installed_version()
    if not check:
        payload = {"installed_version": installed_version}
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(f"longhouse {installed_version}")
        return

    try:
        result = check_for_updates()
    except Exception as exc:
        payload = {
            "installed_version": installed_version,
            "error": str(exc),
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.secho(f"Update check failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = {
        "installed_version": result.installed_version,
        "latest_version": result.latest_version,
        "update_available": result.update_available,
        "install_method": result.install_method,
        "install_source": result.install_source,
        "upgrade_command": result.upgrade_command,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"Installed: {result.installed_version}")
    typer.echo(f"Latest:    {result.latest_version}")
    if result.update_available:
        typer.secho("Update available.", fg=typer.colors.YELLOW)
        typer.echo(f"Run: {result.upgrade_command}")
    else:
        typer.secho("Longhouse is up to date.", fg=typer.colors.GREEN)


def upgrade_command(
    package_source: str | None = typer.Option(
        None,
        "--package-source",
        hidden=True,
        help="Override package source for testing or local release validation.",
    ),
) -> None:
    """Upgrade the Longhouse CLI using the recorded install method."""

    install_metadata = detect_install_metadata()
    package_name = install_metadata.package_name or PACKAGE_NAME
    if install_metadata.install_method != "uv":
        typer.secho(
            f"Unsupported automatic upgrade path for install method '{install_metadata.install_method}'.",
            fg=typer.colors.RED,
        )
        typer.echo(f"Recommended command: {recommended_upgrade_command(install_metadata)}")
        raise typer.Exit(code=1)

    if package_source and package_source.strip():
        normalized_source = package_source.strip()
        typer.echo(f"Upgrading Longhouse from override source: {normalized_source}")
        try:
            subprocess.run(["uv", "tool", "uninstall", package_name], check=False)
            completed = subprocess.run(
                ["uv", "tool", "install", "--force", "--no-cache", normalized_source],
                check=False,
            )
        except FileNotFoundError:
            typer.secho("uv is not installed or not on PATH.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        install_source = "custom"
        package_ref = normalized_source
    else:
        typer.echo(f"Upgrading Longhouse via uv: {package_name}")
        try:
            completed = subprocess.run(["uv", "tool", "upgrade", package_name], check=False)
        except FileNotFoundError:
            typer.secho("uv is not installed or not on PATH.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        refreshed_install_metadata = detect_install_metadata()
        install_source = refreshed_install_metadata.install_source
        package_ref = refreshed_install_metadata.package_ref

    if completed.returncode != 0:
        raise typer.Exit(code=completed.returncode)

    updated_metadata = write_install_metadata(
        install_method="uv",
        install_source=install_source,
        package_name=package_name,
        channel=install_metadata.channel or DEFAULT_CHANNEL,
        package_ref=package_ref,
    )
    typer.secho(f"Longhouse {updated_metadata.installed_version} is installed.", fg=typer.colors.GREEN)

    _reconcile_runtime_after_upgrade()


def _reconcile_runtime_after_upgrade() -> None:
    """Refresh engine, Codex, hooks, and desktop app from the newly installed CLI.

    Runs `longhouse connect --install` as a subprocess so the upgraded package is
    loaded in a fresh interpreter. If the machine has never been connected, the
    reconcile raises and we surface a short hint instead of forcing config.
    """

    longhouse_bin = _resolve_longhouse_entrypoint()
    if longhouse_bin is None:
        typer.echo("Skipping runtime refresh: could not locate the longhouse entrypoint.")
        typer.echo("Run: longhouse connect --install")
        return

    typer.echo("Refreshing engine, Codex, hooks, and desktop app...")
    completed = subprocess.run(
        [longhouse_bin, "connect", "--install"],
        check=False,
    )
    if completed.returncode != 0:
        typer.secho(
            "Runtime refresh failed; CLI is upgraded but engine/Codex may be stale.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Re-run when ready: longhouse connect --install")
        return

    typer.echo("Verify with: longhouse doctor")


def _resolve_longhouse_entrypoint() -> str | None:
    """Return the filesystem path of the installed `longhouse` CLI entrypoint."""

    candidate = sys.argv[0] if sys.argv else ""
    if candidate and Path(candidate).name.startswith("longhouse"):
        resolved = Path(candidate).resolve()
        if resolved.exists():
            return str(resolved)

    from shutil import which

    discovered = which("longhouse")
    return discovered


def record_install_command(
    install_method: str = typer.Option(..., "--install-method"),
    install_source: str = typer.Option(..., "--install-source"),
    package_name: str = typer.Option(PACKAGE_NAME, "--package-name"),
    channel: str = typer.Option(DEFAULT_CHANNEL, "--channel"),
    package_ref: str | None = typer.Option(None, "--package-ref"),
) -> None:
    """Write local install metadata for installer-managed Longhouse setups."""

    metadata_payload = write_install_metadata(
        install_method=install_method,
        install_source=install_source,
        package_name=package_name,
        channel=channel,
        package_ref=package_ref,
    )
    typer.echo(json.dumps(asdict(metadata_payload), indent=2))
