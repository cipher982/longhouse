"""Compatibility commands for native managed-local Claude channel sessions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
    run_id: str | None = typer.Option(None, "--run-id", envvar="LONGHOUSE_RUN_ID"),
    provider_session_id: str | None = typer.Option(
        None,
        "--provider-session-id",
        envvar="LONGHOUSE_PROVIDER_SESSION_ID",
    ),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    port: int = typer.Option(0, "--port", min=0, max=65535),
    claude_pid: int | None = typer.Option(None, "--claude-pid", envvar="LONGHOUSE_CHANNEL_PARENT_PID"),
    cwd: str | None = typer.Option(None, "--cwd", envvar="LONGHOUSE_CHANNEL_CWD"),
) -> None:
    """Run the native local MCP channel bridge that Claude connects to over stdio."""

    argv = _engine_command("serve")
    _append_option(argv, "--session-id", session_id)
    _append_option(argv, "--run-id", run_id)
    _append_option(argv, "--provider-session-id", provider_session_id)
    _append_option(argv, "--state-root", state_root)
    _append_option(argv, "--port", port)
    _append_option(argv, "--claude-pid", claude_pid)
    _append_option(argv, "--cwd", cwd)
    _exec_engine(argv, os.environ.copy())


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
