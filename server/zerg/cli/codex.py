"""Managed-local Codex launcher CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import typer

from zerg.cli import claude as managed_local_cli
from zerg.services.session_continuity import get_machine_name_label
from zerg.services.shipper.service import get_engine_executable
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_loop_mode import SessionLoopMode

ManagedLocalLaunchResponse = managed_local_cli.ManagedLocalLaunchResponse
_interactive_stdio = managed_local_cli._interactive_stdio
_load_api_credentials = managed_local_cli._load_api_credentials
_run_attach_command = managed_local_cli._run_attach_command
_finalize_managed_local_launch = managed_local_cli._finalize_managed_local_launch
_build_session_url = managed_local_cli._build_session_url
_open_session_url = managed_local_cli._open_session_url


def _check_codex_binary() -> str | None:
    return shutil.which("codex")


def _start_native_codex_bridge(
    *,
    session_id: str,
    cwd: Path,
    url: str,
    token: str,
) -> tuple[str, str]:
    try:
        engine = get_engine_executable()
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    completed = subprocess.run(
        [
            engine,
            "codex-bridge",
            "start",
            "--session-id",
            session_id,
            "--cwd",
            str(cwd),
            "--url",
            url,
            "--token",
            token,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or (completed.stdout or "").strip() or "Failed to start native Codex bridge"
        typer.secho(detail, fg=typer.colors.RED)
        raise typer.Exit(code=1)
    try:
        payload = json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        typer.secho(f"Failed to parse native Codex bridge output: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    thread_id = str(payload.get("thread_id") or "").strip()
    ws_url = str(payload.get("ws_url") or "").strip()
    if not thread_id or not ws_url:
        typer.secho("Native Codex bridge did not return thread_id/ws_url", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return thread_id, ws_url


def _run_native_codex_tui(*, thread_id: str, ws_url: str, cwd: Path) -> int:
    codex_bin = _check_codex_binary()
    if not codex_bin:
        typer.secho("Session launch requires the 'codex' CLI but it is not installed.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    completed = subprocess.run(
        [
            codex_bin,
            "resume",
            thread_id,
            "--enable",
            "tui_app_server",
            "--remote",
            ws_url,
        ],
        check=False,
        cwd=str(cwd),
        env=os.environ.copy(),
    )
    return int(completed.returncode)


def _launch_managed_local_from_api(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    loop_mode: SessionLoopMode,
    name: str | None,
    machine_name: str,
    managed_transport: ManagedSessionTransport,
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
        managed_transport=managed_transport,
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
    transport: ManagedSessionTransport = typer.Option(
        ManagedSessionTransport.CODEX_APP_SERVER,
        "--transport",
        help="Managed local transport: codex_app_server (native) or tmux (fallback).",
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
        managed_transport=transport,
    )
    if result.managed_transport == ManagedSessionTransport.TMUX:
        _finalize_managed_local_launch(
            provider_label="Codex",
            base_url=resolved_url,
            result=result,
            open_browser=open_browser,
            attach=attach,
        )
        return

    session_url = _build_session_url(resolved_url, result.session_id)
    typer.secho("Managed local Codex session launched on this device.", fg=typer.colors.GREEN)
    typer.echo(f"Session ID: {result.session_id}")
    typer.echo(f"Provider session ID: {result.provider_session_id}")
    typer.echo(f"Session URL: {session_url}")
    typer.echo("Starting native Codex bridge...")
    thread_id, ws_url = _start_native_codex_bridge(
        session_id=result.session_id,
        cwd=cwd,
        url=resolved_url,
        token=resolved_token,
    )
    typer.echo(f"Codex thread: {thread_id}")
    typer.echo(f"Remote target: {ws_url}")

    if open_browser:
        typer.echo("Opening session in browser...")
        if not _open_session_url(session_url):
            typer.secho(f"Could not open browser automatically. Visit: {session_url}", fg=typer.colors.YELLOW)

    if not attach:
        typer.echo("Attach: " + f"codex resume {thread_id} --enable tui_app_server --remote {ws_url}")
        return
    if not _interactive_stdio():
        typer.secho("Skipping auto-attach because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        typer.echo("Attach: " + f"codex resume {thread_id} --enable tui_app_server --remote {ws_url}")
        return

    typer.echo("Attaching...")
    exit_code = _run_native_codex_tui(thread_id=thread_id, ws_url=ws_url, cwd=cwd)
    if exit_code != 0:
        typer.secho(
            f"Auto-attach exited with code {exit_code}. Run the printed remote resume command manually.",
            fg=typer.colors.YELLOW,
        )
