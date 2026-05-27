"""Shared helpers for CLI modules."""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import typer

from zerg.services.local_health import collect_launch_readiness
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token


@dataclass(frozen=True)
class ManagedLocalLaunchResponse:
    session_id: str
    provider_session_id: str
    attach_command: str
    source_runner_name: str
    managed_transport: str | None = None


def load_api_credentials(
    *,
    url: str | None,
    token: str | None,
    config_dir: Path | None,
    exit_code: int = 1,
    config_dir_is_provider_home: bool = False,
    resolve_url: Callable[[Path | None], str | None] = get_zerg_url,
    resolve_token: Callable[[Path | None], str | None] = load_token,
) -> tuple[str, str]:
    state_root = resolve_longhouse_home_from_provider_home(config_dir) if config_dir_is_provider_home and config_dir else config_dir
    resolved_url = (url or resolve_url(state_root) or "").strip()
    if not resolved_url:
        typer.secho("No Longhouse URL configured. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=exit_code)

    resolved_token = (token or resolve_token(state_root) or "").strip()
    if not resolved_token:
        typer.secho("No device token found. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=exit_code)

    return resolved_url, resolved_token


def ensure_managed_launch_preflight(
    *,
    url: str,
    machine_name: str,
    config_dir: Path | None,
    config_dir_is_provider_home: bool = True,
    exit_code: int = 1,
) -> None:
    """Fail fast when the local machine contract disagrees with managed launch."""

    state_root = resolve_longhouse_home_from_provider_home(config_dir) if config_dir_is_provider_home and config_dir else config_dir
    readiness = collect_launch_readiness(
        state_root,
        runtime_url_override=url,
        machine_name_override=machine_name,
    )
    reasons = {str(item) for item in list(readiness.get("reasons") or [])}
    actionable = {
        "config_url_runner_url_mismatch",
        "machine_name_runner_name_mismatch",
        "service_runner_name_mismatch",
    }
    if not reasons.intersection(actionable):
        return

    runner = dict(readiness.get("runner") or {})
    runner_urls = ", ".join(str(item) for item in list(runner.get("runner_urls") or []) if str(item).strip()) or "-"
    runner_name = str(runner.get("runner_name") or "").strip() or "-"
    stored_url = str(readiness.get("stored_url") or "").strip() or "-"

    typer.secho("Managed launch config is inconsistent on this machine.", fg=typer.colors.RED)
    typer.echo(f"  launch target: {readiness.get('control_plane_url') or url}")
    if stored_url != str(readiness.get("control_plane_url") or url):
        typer.echo(f"  stored target: {stored_url}")
    typer.echo(f"  remote command Runner target: {runner_urls}")
    typer.echo(f"  launch machine: {readiness.get('machine_name') or machine_name}")
    typer.echo(f"  remote command Runner name: {runner_name}")
    typer.echo("  Fix: longhouse machine configure --url <control-plane-url> --machine-name <runner-name>")
    typer.echo("  Note: this Runner is separate from the Machine Agent that ships transcripts.")
    typer.echo("  Scratch local work: LONGHOUSE_HOME=~/.longhouse-dev ...")
    raise typer.Exit(code=exit_code)


def parse_uuid_or_exit(
    raw: str | None,
    *,
    label: str,
    missing_message: str | None = None,
) -> str:
    value = str(raw or "").strip()
    if not value:
        typer.secho(missing_message or f"{label} is required.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        return str(UUID(value))
    except ValueError:
        typer.secho(f"{label} must be a valid UUID.", fg=typer.colors.RED)
        raise typer.Exit(code=1)


def git_output(cwd: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def interactive_stdio() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def build_session_url(url: str, session_id: str) -> str:
    return f"{url.rstrip('/')}/timeline/{session_id}"


def open_session_url(session_url: str) -> bool:
    try:
        return bool(webbrowser.open(session_url))
    except Exception:
        return False
