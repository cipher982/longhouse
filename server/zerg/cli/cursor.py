"""Longhouse CLI for the Cursor agent harness.

Default invocation (``longhouse cursor``) launches a Cursor **Helm** session:
an invisible interactive ``cursor-agent`` TUI with a background remote-control
channel, matching the claude/codex/opencode managed pattern. See
:mod:`zerg.cli.cursor_helm` for the PTY pass-through + per-session socket
mechanism.

``longhouse cursor decode`` is a local diagnostic path. It never uploads
Cursor data; it is retained only for source-format inspection while the native
storage-v2 source adapter is built.

See ``docs/specs/cursor-transcript-format.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from zerg.session_loop_mode import SessionLoopMode

app = typer.Typer(
    help="Cursor agent harness: managed Helm launch + unmanaged ingest/inspection.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.command(name="binding-probe")
def binding_probe(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse Cursor Helm session UUID."),
    phase: str = typer.Option(..., "--phase", help="before_launch, after_prompt, after_tool_turn, or at_exit."),
    store_db: Path | None = typer.Option(None, "--store-db", help="Controlled launch's Cursor store.db (required after launch)."),
) -> None:
    """Record one interactive, read-only Cursor Helm binding-probe observation."""
    from zerg.services.cursor_binding_probe import record_probe_observation

    try:
        artifact = record_probe_observation(session_id, phase, store_db)
    except (OSError, ValueError) as exc:
        typer.secho(f"Cursor Helm binding probe failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"status": artifact["status"], "artifact": artifact["artifact_path"]}, sort_keys=True))
    if artifact["status"] != "passed":
        raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def launch(
    cwd: Path = typer.Option(
        Path("."),
        "--cwd",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Working directory to launch cursor-agent from (defaults to current directory).",
    ),
    project: str | None = typer.Option(None, "--project", help="Optional session project label."),
    loop_mode: SessionLoopMode = typer.Option(
        SessionLoopMode.ASSIST,
        "--loop-mode",
        help="Loop mode to store on the Longhouse session.",
    ),
    name: str | None = typer.Option(None, "--name", help="Optional display name for the session."),
    resume_session: str | None = typer.Option(
        None,
        "--resume-session",
        help="Resume a stopped Cursor Helm conversation by its Longhouse session UUID.",
    ),
    url: str | None = typer.Option(None, "--url", "-u", help="Longhouse API URL (uses stored URL if not specified)."),
    token: str | None = typer.Option(None, "--token", "-t", help="Device token (uses stored token if not specified)."),
    config_dir: str | None = typer.Option(
        None,
        "--config-dir",
        help="Longhouse config directory (default: ~/.longhouse).",
    ),
    verbose: bool = typer.Option(False, "--verbose/--quiet", "-v", help="Show session id + timeline URL on launch."),
    open_browser: bool = typer.Option(False, "--open/--no-open", help="Print the timeline URL after the session ends."),
    cursor_args: list[str] = typer.Argument(
        None,
        help="Extra args forwarded to cursor-agent (use '--' to separate).",
    ),
) -> None:
    """Launch a Longhouse Cursor Helm session: interactive cursor-agent TUI + remote steer."""
    from zerg.cli.cursor_helm import run_helm

    run_helm(
        cwd=cwd,
        project=project,
        name=name,
        loop_mode=loop_mode,
        url=url,
        token=token,
        config_dir=config_dir,
        permission_mode="bypass",
        cursor_args=cursor_args,
        verbose=verbose,
        open_browser=open_browser,
        resume_session_id=resume_session,
    )


@app.command(name="decode")
def decode(
    store_db: Path = typer.Argument(..., help="Path to a Cursor store.db file."),
    json_output: bool = typer.Option(False, "--json", help="Emit full decoded events as JSON."),
) -> None:
    """Decode one Cursor store.db locally for inspection (no server contact)."""
    from zerg.services.cursor_transcript import decode_store_db

    result = decode_store_db(store_db)
    diag = result.diagnostics
    if result.session is None:
        typer.secho(
            f"unsupported_gap={diag.unsupported_gap}  reason={diag.unsupported_reason}",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(result.session.model_dump_json(indent=2))
        return
    typer.secho(f"provider_session_id={result.session.provider_session_id}", fg=typer.colors.CYAN)
    typer.secho(f"title={diag.title}  model={diag.model}  workspace={diag.workspace}")
    typer.secho(f"messages={diag.message_count}  events={diag.event_count}")
    if diag.unknown_block_types:
        typer.secho(f"unknown_block_types={diag.unknown_block_types}", fg=typer.colors.YELLOW)
    for event in result.session.events:
        label = event.role
        if event.tool_name:
            label = f"{event.role}/tool:{event.tool_name}"
        preview = (event.content_text or event.tool_output_text or "").replace("\n", " ")[:80]
        typer.echo(f"  [{label}] {event.timestamp.isoformat()}  {preview}")
