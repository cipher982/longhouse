"""Managed-local Claude launcher CLI."""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import typer

from zerg.services.session_continuity import get_machine_name_label
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.session_loop_mode import SessionLoopMode


@dataclass(frozen=True)
class ManagedLocalLaunchResponse:
    session_id: str
    provider_session_id: str
    attach_command: str
    source_runner_name: str


def _interactive_stdio() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_attach_command(attach_command: str) -> int:
    parts = shlex.split(attach_command)
    completed = subprocess.run(parts, check=False)
    return int(completed.returncode)


def _load_api_credentials(
    *,
    url: str | None,
    token: str | None,
    config_dir: Path | None,
) -> tuple[str, str]:
    resolved_url = (url or get_zerg_url(config_dir) or "").strip()
    if not resolved_url:
        typer.secho("No Longhouse URL configured. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    resolved_token = (token or load_token(config_dir) or "").strip()
    if not resolved_token:
        typer.secho("No device token found. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    return resolved_url, resolved_token


def _git_output(cwd: Path, *args: str) -> str | None:
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


def _infer_git_context(cwd: Path) -> tuple[str | None, str | None]:
    git_repo = _git_output(cwd, "rev-parse", "--show-toplevel")
    git_branch = _git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if git_branch == "HEAD":
        git_branch = None
    return git_repo, git_branch


def _launch_managed_local_from_api(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    loop_mode: SessionLoopMode,
    name: str | None,
    machine_name: str,
    provider: str = "claude",
) -> ManagedLocalLaunchResponse:
    git_repo, git_branch = _infer_git_context(cwd)
    payload = {
        "cwd": str(cwd),
        "provider": provider,
        "project": project,
        "git_repo": git_repo,
        "git_branch": git_branch,
        "display_name": name,
        "loop_mode": loop_mode.value,
        "machine_name": machine_name,
    }

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{url.rstrip('/')}/api/sessions/managed-local/this-device",
                headers={"X-Agents-Token": token},
                json=payload,
            )
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        typer.secho(f"Request timed out connecting to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if response.status_code == 401:
        typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if response.status_code != 200:
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("detail") or "").strip()
        except ValueError:
            detail = response.text.strip()
        message = detail or response.text[:200] or "Managed local launch failed"
        typer.secho(message, fg=typer.colors.RED)
        raise typer.Exit(code=1)

    body = response.json()
    return ManagedLocalLaunchResponse(
        session_id=str(body["session_id"]),
        provider_session_id=str(body["provider_session_id"]),
        attach_command=str(body["attach_command"]),
        source_runner_name=str(body.get("source_runner_name") or machine_name),
    )


def claude(
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
    name: str | None = typer.Option(None, "--name", help="Optional display name for the Claude session."),
    attach: bool = typer.Option(
        True,
        "--attach/--no-attach",
        help="Auto-attach to the managed local session when running interactively.",
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
) -> None:
    """Launch a managed-local Claude Code session on this device via the Longhouse API."""

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
        provider="claude",
    )

    typer.secho("Managed local Claude session launched on this device.", fg=typer.colors.GREEN)
    typer.echo(f"Session ID: {result.session_id}")
    typer.echo(f"Provider session ID: {result.provider_session_id}")
    typer.echo(f"Attach: {result.attach_command}")

    if not attach:
        return
    if not _interactive_stdio():
        typer.secho("Skipping auto-attach because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        return

    typer.echo("Attaching...")
    exit_code = _run_attach_command(result.attach_command)
    if exit_code != 0:
        typer.secho(
            f"Auto-attach exited with code {exit_code}. Run the printed attach command manually.",
            fg=typer.colors.YELLOW,
        )
