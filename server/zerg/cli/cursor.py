"""Longhouse CLI for the Cursor agent harness.

Two surfaces:

* ``longhouse cursor <prompt...>`` — **managed** wrapper (default callback).
  Launches stock ``cursor-agent --print --output-format stream-json``, streams
  stdout to the terminal, parses each event with
  :mod:`zerg.services.cursor_stream` (real per-event ``timestamp_ms``), and
  ingests the session into the Runtime Host through ``/api/agents/ingest``.
  ``--resume <chatId>`` continues an existing Cursor chat (the honest
  send/continue path). SIGINT is forwarded to the cursor-agent child process
  group (the interrupt path). See
  ``docs/specs/cursor-transcript-format.md`` and the managed-provider-cli
  skill for the capability contract.

* ``longhouse cursor import`` / ``longhouse cursor decode`` — **unmanaged**
  ingest + inspection of on-disk ``~/.cursor/chats`` ``store.db`` sessions via
  :mod:`zerg.services.cursor_transcript`.

Longhouse owns the wrapper/control path; the ``cursor-agent`` binary remains
user-owned (resolved from PATH, overridable via ``LONGHOUSE_CURSOR_BIN``).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import typer

from zerg.cli._common import load_api_credentials
from zerg.services.cursor_stream import CursorStreamBuilder
from zerg.services.cursor_transcript import decode_store_db
from zerg.services.cursor_transcript import iter_local_cursor_stores
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token

app = typer.Typer(help="Cursor agent harness: managed wrapper + unmanaged ingest.", no_args_is_help=True)

PROVIDER_BIN_ENV = "LONGHOUSE_CURSOR_BIN"
DEFAULT_BIN = "cursor-agent"


# ---------------------------------------------------------------------------
# Shared credential helper
# ---------------------------------------------------------------------------


def _load_creds(url: str | None, token: str | None, config_dir: Path | None) -> tuple[str, str]:
    return load_api_credentials(
        url=url,
        token=token,
        config_dir=config_dir,
        resolve_url=get_zerg_url,
        resolve_token=load_token,
    )


def _resolve_cursor_bin(explicit: str | None) -> str:
    if explicit:
        return explicit
    env_bin = os.environ.get(PROVIDER_BIN_ENV)
    if env_bin:
        return env_bin
    found = shutil.which(DEFAULT_BIN)
    if found:
        return found
    raise typer.Exit(
        typer.secho(
            f"cursor-agent binary not found on PATH (override with --cursor-bin or ${PROVIDER_BIN_ENV})",
            fg=typer.colors.RED,
        )
    )


# ---------------------------------------------------------------------------
# Managed wrapper (default callback)
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def cursor(
    ctx: typer.Context,
    prompt: list[str] = typer.Argument(None, help="Prompt for the cursor-agent run."),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help="Resume (continue) an existing Cursor chat by chatId. This is the managed send/continue path.",
    ),
    model: str | None = typer.Option(None, "--model", "-m", help="Cursor model name (e.g. gpt-5.2)."),
    cwd: Path = typer.Option(
        Path.cwd(),
        "--cwd",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace directory to run in (passed as --workspace).",
    ),
    cursor_bin: str | None = typer.Option(
        None,
        "--cursor-bin",
        help=f"Path to cursor-agent binary (default: PATH; env {PROVIDER_BIN_ENV}).",
    ),
    yolo: bool = typer.Option(
        True,
        "--yolo/--no-yolo",
        help="Auto-approve tool calls (--force). On by default for managed runs.",
    ),
    url: str | None = typer.Option(None, "--url", "-u", help="Runtime Host base URL."),
    token: str | None = typer.Option(None, "--token", "-t", help="Device token."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Stream + parse locally but do not POST to the Runtime Host.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Do not passthrough stream-json to stdout (still ingests).",
    ),
) -> None:
    """Managed wrapper: run cursor-agent headless, stream, and ingest."""
    if ctx.invoked_subcommand is not None:
        return  # a subcommand (import/decode) is handling this invocation

    prompt_text = " ".join(prompt).strip() if prompt else ""
    if not prompt_text and not resume:
        typer.secho("Provide a prompt, or --resume <chatId> with a prompt to continue.", fg=typer.colors.YELLOW)
        raise typer.Exit()
    if resume and not prompt_text:
        typer.secho("--resume requires a follow-up prompt to send.", fg=typer.colors.YELLOW)
        raise typer.Exit()

    binary = _resolve_cursor_bin(cursor_bin)

    cmd: list[str] = [binary, "--print", "--output-format", "stream-json", "--trust"]
    if yolo:
        cmd.append("--yolo")
    if model:
        cmd.extend(["--model", model])
    if resume:
        cmd.extend(["--resume", resume])
    cmd.extend(["--workspace", str(cwd)])
    if prompt_text:
        cmd.append(prompt_text)

    if dry_run:
        base_url = ""
        resolved_token = ""
    else:
        base_url, resolved_token = _load_creds(url=url, token=token, config_dir=None)

    builder = CursorStreamBuilder(
        device_id=os.environ.get("LONGHOUSE_DEVICE_ID"),
        device_name=os.environ.get("LONGHOUSE_DEVICE_NAME"),
    )

    typer.secho(f"launching: {' '.join(cmd[:4])} ... (cwd={cwd})", fg=typer.colors.CYAN, err=True)

    child = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        cwd=str(cwd),
        start_new_session=True,  # new session/process group so we can signal it
        text=True,
        bufsize=1,
    )

    interrupted = False

    def _forward(signum: int, _frame) -> None:
        nonlocal interrupted
        interrupted = True
        try:
            os.killpg(os.getpgid(child.pid), signum)
        except (ProcessLookupError, PermissionError):
            pass

    prev_int = signal.signal(signal.SIGINT, _forward)
    prev_term = signal.signal(signal.SIGTERM, _forward)

    exit_code = 0
    try:
        assert child.stdout is not None
        for line in child.stdout:
            if not quiet:
                sys.stdout.write(line)
                sys.stdout.flush()
            builder.feed_line(line)
        exit_code = child.wait()
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        if child.poll() is None:
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            child.wait(timeout=5)

    session, diag = builder.build(), builder.diag

    # Ingest (unless dry-run). Re-posting the same session is idempotent via
    # event-hash dedup on the server, so a partial-then-final post is safe.
    ingested = False
    if not dry_run and diag.event_count > 0:
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    f"{base_url.rstrip('/')}/api/agents/ingest",
                    headers={"X-Agents-Token": resolved_token, "Content-Type": "application/json"},
                    content=session.model_dump_json(),
                )
        except httpx.HTTPError as exc:
            typer.secho(f"ingest error: {exc}", fg=typer.colors.RED, err=True)
        else:
            if resp.status_code >= 400:
                typer.secho(f"ingest failed: HTTP {resp.status_code}", fg=typer.colors.RED, err=True)
            else:
                ingested = True

    typer.secho(
        f"cursor session_id={diag.session_id} events={diag.event_count} "
        f"fidelity={diag.timestamp_fidelity} ingested={ingested} "
        f"exit={exit_code} interrupted={interrupted}",
        fg=typer.colors.GREEN if ingested or dry_run else typer.colors.YELLOW,
        err=True,
    )

    raise typer.Exit(code=exit_code)


# ---------------------------------------------------------------------------
# Unmanaged: import + decode (store.db)
# ---------------------------------------------------------------------------


def _scan_stores(cursor_dir: Path | None) -> list[Path]:
    return sorted(iter_local_cursor_stores(cursor_dir), key=lambda p: p.stat().st_mtime, reverse=True)


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
