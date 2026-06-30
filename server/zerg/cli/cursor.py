"""Longhouse CLI for the Cursor agent harness.

Default invocation (``longhouse cursor``) launches a Cursor **Helm** session:
an invisible interactive ``cursor-agent`` TUI with a background remote-control
channel, matching the claude/codex/opencode managed pattern. See
:mod:`zerg.cli.cursor_helm` for the PTY pass-through + per-session socket
mechanism.

Subcommands:
- ``longhouse cursor import`` scans ``~/.cursor/chats`` for cursor-agent
  ``store.db`` sessions (unmanaged Shadow ingest) and posts canonical
  ``SessionIngest`` to the Runtime Host.
- ``longhouse cursor decode`` is a local debug path that prints what would be
  ingested without contacting the server.

See ``docs/specs/cursor-transcript-format.md``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import typer

from zerg.cli._common import load_api_credentials
from zerg.cli.cursor_helm import run_helm
from zerg.services.cursor_transcript import decode_store_db
from zerg.services.cursor_transcript import iter_local_cursor_stores
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.session_loop_mode import SessionLoopMode

app = typer.Typer(
    help="Cursor agent harness: managed Helm launch + unmanaged ingest/inspection.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _load_creds(url: str | None, token: str | None, config_dir: Path | None) -> tuple[str, str]:
    return load_api_credentials(
        url=url,
        token=token,
        config_dir=config_dir,
        resolve_url=get_zerg_url,
        resolve_token=load_token,
    )


def _scan_stores(cursor_dir: Path | None) -> list[Path]:
    return sorted(iter_local_cursor_stores(cursor_dir), key=lambda p: p.stat().st_mtime, reverse=True)


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
    )


@app.command(name="import")
def import_(
    url: str | None = typer.Option(None, "--url", "-u", help="Runtime Host base URL."),
    token: str | None = typer.Option(None, "--token", "-t", help="Device token (uses stored token if not set)."),
    cursor_dir: Path | None = typer.Option(
        None,
        "--cursor-dir",
        help="Override ~/.cursor root (defaults to ~/.cursor/chats).",
    ),
    limit: int = typer.Option(0, "--limit", help="Max sessions to import (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Decode only; do not POST to the Runtime Host."),
) -> None:
    """Scan local Cursor sessions and ingest them into Longhouse (unmanaged)."""
    stores = _scan_stores(cursor_dir)
    if limit > 0:
        stores = stores[:limit]
    if not stores:
        typer.secho("No Cursor store.db sessions found under ~/.cursor/chats.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    base_url = ""
    resolved_token = ""
    if not dry_run:
        base_url, resolved_token = _load_creds(url=url, token=token, config_dir=None)

    ingested = 0
    skipped_gap = 0
    failed = 0
    for store_path in stores:
        result = decode_store_db(store_path)
        title = result.diagnostics.title or store_path.parent.name
        if result.session is None:
            gap = result.diagnostics.unsupported_gap or "unknown"
            skipped_gap += 1
            typer.secho(
                f"SKIP  {store_path.parent.name}  [{gap}]  {title}",
                fg=typer.colors.YELLOW,
            )
            continue
        if dry_run:
            typer.secho(
                f"DRY   {store_path.parent.name}  msgs={result.diagnostics.message_count} "
                f"events={result.diagnostics.event_count}  {title}",
                fg=typer.colors.CYAN,
            )
            ingested += 1
            continue
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    f"{base_url.rstrip('/')}/api/agents/ingest",
                    headers={"X-Agents-Token": resolved_token, "Content-Type": "application/json"},
                    content=result.session.model_dump_json(),
                )
        except httpx.HTTPError as exc:
            failed += 1
            typer.secho(f"ERROR {store_path.parent.name}  {exc}", fg=typer.colors.RED)
            continue
        if resp.status_code >= 400:
            failed += 1
            typer.secho(
                f"FAIL  {store_path.parent.name}  HTTP {resp.status_code}  {title}",
                fg=typer.colors.RED,
            )
            continue
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        inserted = body.get("events_inserted", "?")
        typer.secho(
            f"OK    {store_path.parent.name}  inserted={inserted}  {title}",
            fg=typer.colors.GREEN,
        )
        ingested += 1

    typer.echo("")
    typer.secho(
        f"done: ingested={ingested} skipped_gap={skipped_gap} failed={failed} " f"(dry_run={dry_run})",
        fg=typer.colors.CYAN,
        bold=True,
    )


@app.command(name="decode")
def decode(
    store_db: Path = typer.Argument(..., help="Path to a Cursor store.db file."),
    json_output: bool = typer.Option(False, "--json", help="Emit full decoded events as JSON."),
) -> None:
    """Decode one Cursor store.db locally for inspection (no server contact)."""
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
