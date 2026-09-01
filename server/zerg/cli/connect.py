"""CLI helpers for shipping sessions and recalling them from the terminal.

Device setup — `auth`, `connect`, machine-agent install/uninstall/status — moved
to the native `longhouse` facade (`engine/src/longhouse.rs`). What remains here
is Runtime Host work: driving the engine's one-shot ship and reading the recall
API.
"""

from __future__ import annotations

import json as json_lib
import logging
import os
import subprocess
from pathlib import Path

import httpx
import typer

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.services.shipper.service import get_engine_executable
from zerg.services.shipper.token import normalize_zerg_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def _resolve_configured_url(url: object | None, config_dir: Path | None) -> str:
    explicit_url = normalize_zerg_url(url)
    if explicit_url:
        return explicit_url

    stored_url = normalize_zerg_url(get_zerg_url(config_dir))
    if stored_url:
        return stored_url

    typer.secho("No Longhouse URL configured.", fg=typer.colors.RED)
    typer.echo(
        "Run `longhouse-server onboard` for a local setup, `longhouse auth --url <url>` for a remote instance, or pass `--url` explicitly."
    )
    raise typer.Exit(code=1)


def ship(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        help="Device token (uses stored token if not specified)",
    ),
    file: str = typer.Option(
        None,
        "--file",
        "-f",
        help="Ship a single session JSONL file (used by hooks)",
    ),
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        "-d",
        help="Claude config directory (default: ~/.claude)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress output (for hook usage)",
    ),
) -> None:
    """One-shot: ship all new Claude Code sessions to Longhouse.

    Use --file to ship a single session file (designed for Claude Code hook integration).
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None

    url = _resolve_configured_url(url, config_dir)
    if not token:
        token = load_token(config_dir)

    try:
        engine = get_engine_executable()
    except RuntimeError as e:
        if not quiet:
            typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    env = os.environ.copy()
    if verbose:
        env["RUST_LOG"] = "longhouse_engine=debug"
    if claude_dir:
        env["CLAUDE_CONFIG_DIR"] = claude_dir

    # Build base engine args
    engine_args = [engine, "ship"]
    if url:
        engine_args += ["--url", url]
    if token:
        engine_args += ["--token", token]

    # Single-file mode (for Claude Stop hook integration)
    if file:
        file_path = Path(file)
        if not file_path.exists():
            if not quiet:
                typer.secho(f"File not found: {file}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        engine_args += ["--file", str(file_path)]
        stdout = subprocess.DEVNULL if quiet else None
        stderr = subprocess.DEVNULL if quiet else None
        result = subprocess.run(engine_args, env=env, stdout=stdout, stderr=stderr)
        raise typer.Exit(code=result.returncode)

    # Full scan mode
    if not quiet:
        typer.echo(f"Shipping sessions to {url}...")
    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None
    result = subprocess.run(engine_args, env=env, stdout=stdout, stderr=stderr)
    raise typer.Exit(code=result.returncode)


def recall(
    query: str = typer.Argument(..., help="Search query for session content"),
    project: str = typer.Option(
        None,
        "--project",
        "-p",
        help="Filter by project name",
    ),
    provider: str = typer.Option(
        None,
        "--provider",
        help="Filter by provider (claude, codex, antigravity, opencode)",
    ),
    days_back: int = typer.Option(
        14,
        "--days-back",
        "-d",
        min=1,
        max=365,
        help="Days to look back (1-365)",
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        "-n",
        min=1,
        max=10,
        help="Max result cards to return (1-10; default 5)",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output raw JSON response",
    ),
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        help="Device token (uses stored token if not specified)",
    ),
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude)",
    ),
) -> None:
    """Search past sessions from the terminal.

    Queries the Longhouse API for sessions matching a text search,
    and displays results in a readable terminal format.

    Examples:
        longhouse-server recall "auth token refresh"
        longhouse-server recall "database migration" --project zerg --days-back 30
        longhouse-server recall "deploy fix" --json
    """
    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None

    # Load stored credentials if not provided
    if not url:
        url = get_zerg_url(config_dir)
        if not url:
            typer.secho("No Longhouse URL configured. Run 'longhouse auth' first.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    if not token:
        token = load_token(config_dir)
        if not token:
            typer.secho("No device token found. Run 'longhouse auth' first.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    # Build query params
    params: dict = {
        "query": query,
        "since_days": days_back,
        "max_results": limit,
    }
    if project:
        params["project"] = project
    if provider:
        params["provider"] = provider

    # Make API request
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{url.rstrip('/')}/api/agents/recall",
                headers={"X-Agents-Token": token},
                params=params,
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
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    data = response.json()

    # Raw JSON output mode
    if output_json:
        typer.echo(json_lib.dumps(data, separators=(",", ":"), sort_keys=True))
        return

    # Pretty-print results
    matches = data.get("results", [])
    total = data.get("total", 0)

    if not matches:
        typer.echo(f'No recall matches found for "{query}"')
        return

    typer.echo(f'Found {total} recall match{"es" if total != 1 else ""} for "{query}"')
    typer.echo("")

    for i, match in enumerate(matches):
        session_id = str(match.get("session_id") or "")
        project_name = str(match.get("project") or "unknown project")
        provider_name = str(match.get("provider") or "unknown provider")
        started_at = str(match.get("started_at") or "unknown date")
        provenance = _recall_provenance(match.get("matched_by"))
        header = f"  [{i + 1}] {project_name} · {provider_name} · {started_at}"
        typer.secho(header, fg=typer.colors.CYAN, bold=True)
        typer.echo(f"      {url.rstrip('/')}/timeline/{session_id}")
        if provenance:
            typer.echo(f"      matched by: {provenance}")
        matched_turn = str(match.get("matched_tool_name") or match.get("matched_role") or "").strip()
        event_count = int(match.get("total_events") or 0)
        facts = " · ".join(value for value in (matched_turn, f"{event_count} events" if event_count else "") if value)
        if facts:
            typer.echo(f"      {facts}")
        snippet = match.get("snippet")
        if snippet:
            typer.echo(f"      {_recall_excerpt(snippet, max_chars=320)}")
        else:
            typer.echo(f"      snippet unavailable: {match.get('snippet_unavailable_reason') or 'unknown'}")
        result_ref = str(match.get("ref") or "")
        typer.echo(f"      ref: {result_ref}")
        typer.echo(f"      expand: longhouse-server recall-context {result_ref}")

        typer.echo("")


def _recall_turn_label(turn: dict) -> str:
    role = str(turn.get("role") or "unknown")
    tool_name = str(turn.get("tool_name") or "").strip()
    if role == "tool" and tool_name:
        return tool_name
    return role


def _recall_provenance(lanes: object) -> str:
    values = lanes if isinstance(lanes, list) else []
    return " + ".join("semantic" if lane == "dense" else str(lane) for lane in values)


def _recall_excerpt(value: object, *, max_chars: int = 240) -> str:
    content = " ".join(str(value or "").split())
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 3] + "..."


def recall_context(
    ref: str = typer.Argument(..., help="Opaque ref returned by `longhouse recall`"),
    before: int = typer.Option(2, "--before", min=0, max=5, help="Turns before the matching turn (0-5)"),
    after: int = typer.Option(2, "--after", min=0, max=5, help="Turns after the matching turn (0-5)"),
    max_content_bytes: int = typer.Option(
        1_200,
        "--max-content-bytes",
        min=200,
        max=4_000,
        help="Per-turn content ceiling (200-4000; total response stays under 8 KiB)",
    ),
    output_json: bool = typer.Option(False, "--json", "-j", help="Output raw JSON response"),
    url: str = typer.Option(None, "--url", "-u", help="Longhouse API URL (uses stored URL if not specified)"),
    token: str = typer.Option(None, "--token", "-t", help="Device token (uses stored token if not specified)"),
    claude_dir: str = typer.Option(None, "--claude-dir", help="Claude config directory (default: ~/.claude)"),
) -> None:
    """Open one recall result with a bounded conversation window."""

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    url = url or get_zerg_url(config_dir)
    token = token or load_token(config_dir)
    if not url or not token:
        typer.secho("Longhouse URL and device token are required. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{url.rstrip('/')}/api/agents/recall/context",
                headers={"X-Agents-Token": token},
                params={
                    "ref": ref,
                    "before": before,
                    "after": after,
                    "max_content_bytes": max_content_bytes,
                },
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        typer.secho(f"Could not read recall context from {url}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    data = response.json()
    if output_json:
        typer.echo(json_lib.dumps(data, separators=(",", ":"), sort_keys=True))
        return
    typer.secho(f"Session {data.get('session_id', '')}", fg=typer.colors.CYAN, bold=True)
    for turn in data.get("turns", []):
        marker = "*" if turn.get("is_match") else " "
        typer.echo(f"  {marker} [{_recall_turn_label(turn)}] {turn.get('content_text', '')}")
    if not data.get("turns"):
        typer.echo(f"  context unavailable: {data.get('evidence_reason') or 'unknown'}")
    typer.echo(f"\nOpen full session: {url.rstrip('/')}/timeline/{data.get('session_id', '')}")
