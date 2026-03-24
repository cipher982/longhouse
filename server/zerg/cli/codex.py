"""Managed-local Codex launcher CLI."""

from __future__ import annotations

from pathlib import Path

import typer

from zerg.cli import claude as managed_local_cli
from zerg.services.session_continuity import get_machine_name_label
from zerg.session_loop_mode import SessionLoopMode

ManagedLocalLaunchResponse = managed_local_cli.ManagedLocalLaunchResponse
_interactive_stdio = managed_local_cli._interactive_stdio
_load_api_credentials = managed_local_cli._load_api_credentials
_run_attach_command = managed_local_cli._run_attach_command
_finalize_managed_local_launch = managed_local_cli._finalize_managed_local_launch


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
        provider="codex",
    )


def codex(
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
        SessionLoopMode.MANUAL,
        "--loop-mode",
        help="Loop mode to store on the managed-local session.",
    ),
    name: str | None = typer.Option(None, "--name", help="Optional display name for the Codex session."),
    attach: bool = typer.Option(
        True,
        "--attach/--no-attach",
        help="Auto-attach to the managed local session when running interactively.",
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
        "--codex-dir",
        "--claude-dir",
        help="Longhouse config directory (default: ~/.claude).",
    ),
) -> None:
    """Launch a managed-local Codex session on this device via the Longhouse API."""

    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=resolved_config_dir)
    machine_name = get_machine_name_label()
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
    _finalize_managed_local_launch(
        provider_label="Codex",
        base_url=resolved_url,
        result=result,
        open_browser=open_browser,
        attach=attach,
    )
