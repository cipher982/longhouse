"""Shared helpers for CLI modules."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import typer

from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token


def load_api_credentials(
    *,
    url: str | None,
    token: str | None,
    config_dir: Path | None,
    exit_code: int = 1,
    resolve_url: Callable[[Path | None], str | None] = get_zerg_url,
    resolve_token: Callable[[Path | None], str | None] = load_token,
) -> tuple[str, str]:
    resolved_url = (url or resolve_url(config_dir) or "").strip()
    if not resolved_url:
        typer.secho("No Longhouse URL configured. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=exit_code)

    resolved_token = (token or resolve_token(config_dir) or "").strip()
    if not resolved_token:
        typer.secho("No device token found. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=exit_code)

    return resolved_url, resolved_token


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
