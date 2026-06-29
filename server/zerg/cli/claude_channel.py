"""Compatibility commands for native managed-local Claude channel sessions."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import typer

app = typer.Typer(help="Claude channel bridge commands")

_DEFAULT_SEND_WAIT_SECS = 10.0
_ENGINE_BIN_ENV = "LONGHOUSE_ENGINE_BIN"


def _engine_bin() -> str:
    return str(os.environ.get(_ENGINE_BIN_ENV) or "longhouse-engine")


def _append_option(argv: list[str], name: str, value: object | None) -> None:
    if value is None:
        return
    argv.extend([name, str(value)])


def _engine_command(*args: str) -> list[str]:
    return [_engine_bin(), "claude-channel", *args]


def _run_engine(args: list[str]) -> None:
    try:
        completed = subprocess.run(args, check=False, text=True, capture_output=True)
    except FileNotFoundError as exc:
        typer.secho(f"{args[0]} not found; run longhouse connect --install or ensure longhouse-engine is on PATH.", fg=typer.colors.RED)
        raise typer.Exit(code=127) from exc
    if completed.stdout:
        typer.echo(completed.stdout, nl=False)
    if completed.stderr:
        typer.echo(completed.stderr, err=True, nl=False)
    if completed.returncode != 0:
        raise typer.Exit(code=completed.returncode)


def _exec_engine(args: list[str], env: dict[str, str]) -> None:
    try:
        os.execvpe(args[0], args, env)
    except FileNotFoundError as exc:
        typer.secho(f"{args[0]} not found; run longhouse connect --install or ensure longhouse-engine is on PATH.", fg=typer.colors.RED)
        raise typer.Exit(code=127) from exc


@app.command("serve")
def serve(
    session_id: str | None = typer.Option(None, "--session-id", envvar="LONGHOUSE_CHANNEL_SESSION_ID"),
    provider_session_id: str | None = typer.Option(
        None,
        "--provider-session-id",
        envvar="LONGHOUSE_PROVIDER_SESSION_ID",
    ),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    port: int = typer.Option(0, "--port", min=0, max=65535),
    auth_token: str | None = typer.Option(None, "--auth-token", envvar="LONGHOUSE_CHANNEL_AUTH_TOKEN"),
    claude_pid: int | None = typer.Option(None, "--claude-pid", envvar="LONGHOUSE_CHANNEL_PARENT_PID"),
    cwd: str | None = typer.Option(None, "--cwd", envvar="LONGHOUSE_CHANNEL_CWD"),
) -> None:
    """Run the native local MCP channel bridge that Claude connects to over stdio."""

    argv = _engine_command("serve")
    _append_option(argv, "--session-id", session_id)
    _append_option(argv, "--provider-session-id", provider_session_id)
    _append_option(argv, "--state-root", state_root)
    _append_option(argv, "--port", port)
    _append_option(argv, "--claude-pid", claude_pid)
    _append_option(argv, "--cwd", cwd)
    env = os.environ.copy()
    # Preserve the legacy compatibility flag without leaking the token in argv.
    if auth_token:
        env["LONGHOUSE_CHANNEL_AUTH_TOKEN"] = auth_token
    _exec_engine(argv, env)


@app.command("launch", hidden=True)
def launch(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    provider_session_id: str | None = typer.Option(None, "--provider-session-id", help="Claude provider session ID."),
    cwd: Path = typer.Option(..., "--cwd", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    api_url: str = typer.Option(..., "--api-url", help="Longhouse API URL."),
    api_token: str = typer.Option(
        ...,
        "--api-token",
        envvar="LONGHOUSE_CLAUDE_REMOTE_LAUNCH_TOKEN",
        help="Longhouse device token.",
    ),
    claude_dir: Path | None = typer.Option(None, "--claude-dir", file_okay=False, dir_okay=True, resolve_path=True),
    wait_ready_secs: float = typer.Option(20.0, "--wait-ready-secs", min=0.1),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Resume an existing Claude session by id instead of creating a new one.",
    ),
    permission_mode: str = typer.Option(
        "bypass",
        "--permission-mode",
        help="bypass (autonomous, default) or remote_approve (answer permission prompts via Longhouse).",
    ),
    hook_token: str | None = typer.Option(
        None,
        "--hook-token",
        help="Session-scoped hook token for the permission gate (required for remote_approve).",
    ),
) -> None:
    """Launch a detached Claude channel session for the Machine Agent control path."""

    from zerg.cli.claude import _launch_detached_native_claude_channel

    normalized_provider_session_id = str(provider_session_id or "").strip()
    if not normalized_provider_session_id and resume:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "provider_launch_failed",
                        "message": "--provider-session-id is required with --resume",
                    },
                }
            ),
            err=True,
        )
        raise typer.Exit(code=1)
    if not normalized_provider_session_id:
        normalized_provider_session_id = str(uuid4())
    try:
        result = _launch_detached_native_claude_channel(
            session_id=session_id,
            provider_session_id=normalized_provider_session_id,
            cwd=cwd,
            base_url=api_url,
            token=api_token,
            config_dir=claude_dir,
            wait_ready_secs=wait_ready_secs,
            resume=resume,
            permission_mode=permission_mode,
            hook_token=hook_token,
        )
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "provider_launch_failed",
                        "message": str(exc),
                    },
                }
            ),
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(result, default=str))


@app.command("send")
def send(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    text: str = typer.Option(..., "--text", help="Message text to inject into Claude."),
    meta: list[str] | None = typer.Option(None, "--meta", help="Repeatable key=value metadata entries."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_SEND_WAIT_SECS, "--wait-secs", min=0.0),
) -> None:
    """Send a live message into the active native Claude channel bridge."""

    argv = _engine_command("send", "--session-id", session_id, "--text", text, "--wait-secs", str(wait_secs))
    for entry in meta or []:
        argv.extend(["--meta", entry])
    _append_option(argv, "--state-root", state_root)
    _run_engine(argv)


@app.command("interrupt")
def interrupt(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_SEND_WAIT_SECS, "--wait-secs", min=0.0),
) -> None:
    """Send SIGINT to the Claude process associated with the bridge state."""

    argv = _engine_command("interrupt", "--session-id", session_id, "--wait-secs", str(wait_secs))
    _append_option(argv, "--state-root", state_root)
    _run_engine(argv)


@app.command("inspect")
def inspect_state(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_SEND_WAIT_SECS, "--wait-secs", min=0.0),
) -> None:
    """Print the current local bridge state JSON with secrets redacted."""

    argv = _engine_command("inspect", "--session-id", session_id, "--wait-secs", str(wait_secs))
    _append_option(argv, "--state-root", state_root)
    _run_engine(argv)


if __name__ == "__main__":
    app(prog_name="longhouse claude-channel")
