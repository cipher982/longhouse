"""CLI commands for session inspection primitives."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer

from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token

app = typer.Typer(help="Session inspection commands")


def _load_api_credentials(*, url: str | None, token: str | None, config_dir: Path | None) -> tuple[str, str]:
    resolved_url = (url or get_zerg_url(config_dir) or "").strip()
    if not resolved_url:
        typer.secho("No Longhouse URL configured. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    resolved_token = (token or load_token(config_dir) or "").strip()
    if not resolved_token:
        typer.secho("No device token found. Run 'longhouse auth' first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    return resolved_url, resolved_token


def _print_event(event: dict) -> None:
    role = str(event.get("role") or "unknown")
    timestamp = str(event.get("timestamp") or "-")
    tool_name = str(event.get("tool_name") or "").strip()
    content_text = str(event.get("content_text") or "").strip()
    tool_output_text = str(event.get("tool_output_text") or "").strip()

    header = f"[{role}] {timestamp}"
    if tool_name:
        header += f"  tool:{tool_name}"
    typer.secho(header, fg=typer.colors.CYAN, bold=True)

    if content_text:
        typer.echo(content_text)
    if tool_output_text:
        typer.echo(tool_output_text)


@app.command()
def get(
    session_id: str = typer.Argument(..., help="Session UUID."),
    output_json: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output raw JSON response.",
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified).",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="Device token (uses stored token if not specified).",
    ),
    claude_dir: str | None = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude).",
    ),
) -> None:
    """Inspect a single session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/sessions/{session_id}",
                headers={"X-Agents-Token": resolved_token},
            )
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {base_url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        typer.secho(f"Request timed out connecting to {base_url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if response.status_code == 401:
        typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if response.status_code == 404:
        typer.secho(f"Session not found: {session_id}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.secho(str(payload.get("id") or session_id), fg=typer.colors.CYAN, bold=True)
    typer.echo(
        "  provider: {provider}  project: {project}  status: {status}".format(
            provider=payload.get("provider") or "-",
            project=payload.get("project") or "-",
            status=payload.get("status") or "-",
        )
    )
    typer.echo(
        "  started: {started}  branch: {branch}".format(
            started=payload.get("started_at") or "-",
            branch=payload.get("git_branch") or "-",
        )
    )

    git_repo = str(payload.get("git_repo") or "").strip()
    if git_repo:
        typer.echo(f"  repo: {git_repo}")

    summary_title = str(payload.get("summary_title") or "").strip()
    if summary_title:
        typer.echo(f"  title: {summary_title}")

    first_user_message = str(payload.get("first_user_message") or "").strip()
    if first_user_message:
        typer.echo(f"  first user: {first_user_message}")


@app.command()
def events(
    session_id: str = typer.Argument(..., help="Session UUID."),
    roles: str | None = typer.Option(
        None,
        "--roles",
        help="Comma-separated roles filter.",
    ),
    tool_name: str | None = typer.Option(
        None,
        "--tool-name",
        help="Exact tool name filter.",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        help="Content search within the session's events.",
    ),
    context_mode: str = typer.Option(
        "forensic",
        "--context-mode",
        help="Context mode: forensic or active_context.",
    ),
    branch_mode: str = typer.Option(
        "head",
        "--branch-mode",
        help="Branch mode: head or all.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        "-n",
        help="Max events to return.",
    ),
    offset: int = typer.Option(
        0,
        "--offset",
        help="Offset into the event list.",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output raw JSON response.",
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified).",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="Device token (uses stored token if not specified).",
    ),
    claude_dir: str | None = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude).",
    ),
) -> None:
    """Inspect session events with filters."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    params: dict[str, object] = {
        "context_mode": context_mode,
        "branch_mode": branch_mode,
        "limit": limit,
        "offset": offset,
    }
    if roles:
        params["roles"] = roles
    if tool_name:
        params["tool_name"] = tool_name
    if query:
        params["query"] = query

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/sessions/{session_id}/events",
                headers={"X-Agents-Token": resolved_token},
                params=params,
            )
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {base_url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        typer.secho(f"Request timed out connecting to {base_url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if response.status_code == 401:
        typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if response.status_code == 404:
        typer.secho(f"Session not found: {session_id}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    events_payload = list(payload.get("events", []))
    if not events_payload:
        typer.echo(f"No events found for session {session_id}")
        return

    typer.echo(f"Session: {session_id}")
    typer.echo(
        "Events: {total}  branch_mode: {branch_mode}  abandoned: {abandoned}".format(
            total=payload.get("total", len(events_payload)),
            branch_mode=payload.get("branch_mode") or branch_mode,
            abandoned=payload.get("abandoned_events", 0),
        )
    )
    typer.echo("")
    for event in events_payload:
        _print_event(event)
        typer.echo("")
