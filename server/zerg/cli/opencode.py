"""Longhouse OpenCode session launcher CLI."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import typer

from zerg.cli import claude as managed_local_cli
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli._common import build_session_url as _build_session_url
from zerg.cli._common import ensure_managed_launch_preflight as _ensure_managed_launch_preflight
from zerg.cli._common import interactive_stdio as _interactive_stdio
from zerg.cli._common import load_api_credentials as _load_api_credentials
from zerg.cli._common import open_session_url as _open_session_url
from zerg.provider_cli_contract import OPENCODE_BIN_ENV
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_OPENCODE_BIN_FLAG
from zerg.services.session_continuity import get_machine_name_label
from zerg.session_loop_mode import SessionLoopMode

_OPENCODE_BIN_OPTION_HELP = " ".join(
    [
        "Debug override for the OpenCode executable used by managed sessions",
        f"(defaults to {OPENCODE_BIN_ENV}, then `opencode` on PATH).",
    ]
)


class _OpenCodeLaunchError(Exception):
    """Raised when native OpenCode launch preparation fails."""


def _resolve_explicit_opencode_binary(candidate: str, *, source: str) -> str:
    normalized = str(candidate or "").strip()
    if not normalized:
        raise _OpenCodeLaunchError(f"{source} is empty")
    looks_like_path = normalized.startswith((".", "~", "/")) or "/" in normalized or "\\" in normalized
    if looks_like_path:
        path = Path(os.path.expanduser(normalized))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise _OpenCodeLaunchError(f"{source} points to `{candidate}`, but it is not an executable file.")
    resolved = shutil.which(normalized)
    if resolved:
        return resolved
    raise _OpenCodeLaunchError(f"{source} points to `{candidate}`, but it was not found on PATH.")


def _resolve_opencode_binary(explicit: str | None = None) -> str | None:
    normalized = str(explicit or "").strip()
    if normalized:
        return _resolve_explicit_opencode_binary(normalized, source=PROVIDER_CLI_SOURCE_OPENCODE_BIN_FLAG)
    env_candidate = str(os.environ.get(OPENCODE_BIN_ENV) or "").strip()
    if env_candidate:
        return _resolve_explicit_opencode_binary(env_candidate, source=OPENCODE_BIN_ENV)
    return shutil.which("opencode")


def _launch_managed_local_from_api(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    loop_mode: SessionLoopMode,
    name: str | None,
    machine_name: str,
) -> ManagedLocalLaunchResponse:
    return managed_local_cli._launch_managed_local_from_api(
        url=url,
        token=token,
        cwd=cwd,
        project=project,
        loop_mode=loop_mode,
        name=name,
        machine_name=machine_name,
        provider="opencode",
    )


def _build_opencode_command(
    *,
    session_id: str,
    machine_name: str,
    opencode_bin: str,
    cwd: Path,
    opencode_args: tuple[str, ...],
) -> str:
    env_prefix = " ".join(
        [
            f"LONGHOUSE_MANAGED_SESSION_ID={shlex.quote(session_id)}",
            f"LONGHOUSE_DEVICE_ID={shlex.quote(machine_name)}",
        ]
    )
    command = " ".join([shlex.quote(opencode_bin), *(shlex.quote(arg) for arg in opencode_args)])
    return f"cd {shlex.quote(str(cwd))} && {env_prefix} {command}"


def _run_native_opencode(
    *,
    session_id: str,
    machine_name: str,
    opencode_bin: str,
    cwd: Path,
    opencode_args: tuple[str, ...],
) -> int:
    cmd = [opencode_bin, *opencode_args]
    env = os.environ.copy()
    env["LONGHOUSE_MANAGED_SESSION_ID"] = session_id
    env["LONGHOUSE_DEVICE_ID"] = machine_name
    completed = subprocess.run(cmd, check=False, cwd=str(cwd), env=env)
    return int(completed.returncode)


def opencode(
    ctx: typer.Context,
    cwd: Path = typer.Option(
        Path("."),
        "--cwd",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Working directory to launch from (defaults to current directory).",
    ),
    project: str | None = typer.Option(None, "--project", help="Optional session project label."),
    loop_mode: SessionLoopMode = typer.Option(
        SessionLoopMode.ASSIST,
        "--loop-mode",
        help="Loop mode to store on the Longhouse session.",
    ),
    name: str | None = typer.Option(None, "--name", help="Optional display name for the OpenCode session."),
    attach: bool = typer.Option(
        True,
        "--attach/--no-attach",
        help="Launch OpenCode after creating the Longhouse session when running interactively.",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open/--no-open",
        help="Open the session detail page in the default browser after launch.",
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="Device token (uses stored token if not specified)",
    ),
    config_dir: str | None = typer.Option(
        None,
        "--config-dir",
        "--claude-dir",
        help="Longhouse config directory (default: ~/.claude).",
    ),
    opencode_bin: str | None = typer.Option(
        None,
        "--opencode-bin",
        help=_OPENCODE_BIN_OPTION_HELP,
    ),
) -> None:
    """Launch a Longhouse OpenCode session on this machine.

    Extra arguments after the Longhouse options are passed to the stock
    `opencode` executable.
    """

    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = _load_api_credentials(
        url=url,
        token=token,
        config_dir=resolved_config_dir,
        exit_code=managed_local_cli.EXIT_SETUP_FAILED,
    )
    try:
        resolved_opencode_bin = _resolve_opencode_binary(opencode_bin)
    except _OpenCodeLaunchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if not resolved_opencode_bin:
        typer.secho(
            "OpenCode executable not found. Install OpenCode so `opencode` is on PATH, "
            f"or set {OPENCODE_BIN_ENV} / --opencode-bin explicitly for debugging.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    machine_name = get_machine_name_label()
    _ensure_managed_launch_preflight(
        url=resolved_url,
        machine_name=machine_name,
        config_dir=resolved_config_dir,
        exit_code=managed_local_cli.EXIT_SETUP_FAILED,
    )
    typer.echo(f"Longhouse: {resolved_url}")
    result = _launch_managed_local_from_api(
        url=resolved_url,
        token=resolved_token,
        cwd=cwd,
        project=project,
        loop_mode=loop_mode,
        name=name,
        machine_name=machine_name,
    )
    session_url = _build_session_url(resolved_url, result.session_id)
    typer.secho("Longhouse OpenCode session launched on this machine.", fg=typer.colors.GREEN)
    typer.echo(f"Session ID: {result.session_id}")
    typer.echo(f"Session URL: {session_url}")

    if open_browser:
        typer.echo("Opening session in browser...")
        if not _open_session_url(session_url):
            typer.secho(f"Could not open browser automatically. Visit: {session_url}", fg=typer.colors.YELLOW)

    opencode_args = tuple(str(arg) for arg in (ctx.args or ()))
    command = _build_opencode_command(
        session_id=result.session_id,
        machine_name=machine_name,
        opencode_bin=resolved_opencode_bin,
        cwd=cwd,
        opencode_args=opencode_args,
    )
    if not attach:
        typer.echo(f"Run: {command}")
        return
    if not _interactive_stdio():
        typer.secho("Skipping OpenCode launch because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        typer.echo(f"Run: {command}")
        return

    typer.echo("Launching OpenCode...")
    exit_code = _run_native_opencode(
        session_id=result.session_id,
        machine_name=machine_name,
        opencode_bin=resolved_opencode_bin,
        cwd=cwd,
        opencode_args=opencode_args,
    )
    if exit_code != 0:
        typer.secho(f"OpenCode exited with code {exit_code}.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=exit_code)
