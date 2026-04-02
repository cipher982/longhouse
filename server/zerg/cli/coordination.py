"""CLI commands for session coordination primitives."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

import httpx
import typer

from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token

app = typer.Typer(help="Session coordination commands")

_CURRENT_SESSION_ENV = "LONGHOUSE_SESSION_ID"
_CURRENT_SESSION_HEADER = "X-Longhouse-Session-Id"


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


def _parse_uuid_or_exit(raw: str, *, label: str) -> str:
    try:
        return str(UUID(str(raw).strip()))
    except ValueError:
        typer.secho(f"{label} must be a valid UUID.", fg=typer.colors.RED)
        raise typer.Exit(code=1)


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


def _resolve_repo_context(*, explicit_repo: str | None, base_url: str, token: str) -> tuple[str | None, str | None]:
    if explicit_repo:
        return explicit_repo.strip(), None

    current_session_id = ""
    env_session_id = str(os.environ.get(_CURRENT_SESSION_ENV, "") or "").strip()
    if env_session_id:
        try:
            current_session_id = str(UUID(env_session_id))
        except ValueError:
            current_session_id = ""

    if current_session_id:
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(
                    f"{base_url.rstrip('/')}/api/agents/sessions/{current_session_id}",
                    headers={"X-Agents-Token": token},
                )
            if response.status_code == 200:
                payload = response.json()
                git_repo = str(payload.get("git_repo", "") or "").strip()
                if git_repo:
                    return git_repo, current_session_id
        except Exception:
            pass

    cwd = Path.cwd()
    local_repo = _git_output(cwd, "rev-parse", "--show-toplevel")
    if local_repo:
        return local_repo, current_session_id or None

    remote_repo = _git_output(cwd, "config", "--get", "remote.origin.url")
    if remote_repo:
        return remote_repo, current_session_id or None

    return None, current_session_id or None


def _print_peer_summary(peer: dict) -> None:
    session_id = str(peer.get("session_id") or "")
    provider = str(peer.get("provider") or "-")
    device_name = str(peer.get("device_name") or "-")
    presence_state = str(peer.get("presence_state") or "-")
    branch = str(peer.get("git_branch") or "-")
    title = str(peer.get("summary_title") or "").strip()

    typer.secho(session_id, fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  provider: {provider}  device: {device_name}  presence: {presence_state}  branch: {branch}")
    if title:
        typer.echo(f"  {title}")


def _print_tail_event(event: dict) -> None:
    role = str(event.get("role") or "unknown")
    timestamp = str(event.get("timestamp") or "-")
    tool_name = str(event.get("tool_name") or "").strip()
    content = str(event.get("content") or "").strip()

    header = f"[{role}] {timestamp}"
    if tool_name:
        header += f"  tool:{tool_name}"
    typer.secho(header, fg=typer.colors.CYAN, bold=True)
    if content:
        typer.echo(content)


def _resolve_session_context(raw: str | None, *, label: str, guidance: str) -> str:
    value = str(raw or "").strip()
    if not value:
        typer.secho(guidance, fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return _parse_uuid_or_exit(value, label=label)


def _print_message_summary(message: dict) -> None:
    message_id = str(message.get("id") or "-")
    from_session_id = str(message.get("from_session_id") or "-")
    status = str(message.get("delivery_status") or "-")
    created_at = str(message.get("created_at") or "-")
    text = str(message.get("text") or "").strip()

    typer.secho(f"#{message_id}  {status}  {created_at}", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  from: {from_session_id}")
    if text:
        typer.echo(f"  {text}")


@app.command()
def peers(
    repo: str | None = typer.Option(
        None,
        "--repo",
        "-r",
        help="Repo filter (substring match on session git_repo). Defaults to current session repo or local git repo.",
    ),
    active_only: bool = typer.Option(
        True,
        "--active-only/--all",
        help="Show only peers with live presence by default.",
    ),
    days: int = typer.Option(
        7,
        "--days",
        help="Days to look back for repo activity.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max peers to return.",
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
    """List peer sessions working around the same repo."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_repo, current_session_id = _resolve_repo_context(
        explicit_repo=repo,
        base_url=base_url,
        token=resolved_token,
    )
    if not resolved_repo:
        message = "".join(
            [
                "Could not infer a repo. Pass --repo or run the command from a git repo ",
                "or a session with LONGHOUSE_SESSION_ID.",
            ]
        )
        typer.secho(message, fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/sessions/wall",
                headers={"X-Agents-Token": resolved_token},
                params={"repo": resolved_repo, "days": days, "limit": limit},
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
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    peers_payload: list[dict] = []
    for item in payload.get("sessions", []):
        if current_session_id and str(item.get("session_id")) == current_session_id:
            continue
        if active_only and not item.get("has_live_presence"):
            continue
        peers_payload.append(
            {
                "session_id": item.get("session_id"),
                "device_name": item.get("device_name"),
                "provider": item.get("provider"),
                "presence_state": item.get("presence_state"),
                "summary_title": item.get("summary_title"),
                "git_branch": item.get("git_branch"),
            }
        )

    result = {
        "repo": resolved_repo,
        "active_only": active_only,
        "peers": peers_payload,
        "total": len(peers_payload),
    }

    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return

    if not peers_payload:
        typer.echo(f"No peer sessions found for repo filter: {resolved_repo}")
        return

    typer.echo(f"Found {len(peers_payload)} peer session{'s' if len(peers_payload) != 1 else ''}")
    typer.echo(f"Repo: {resolved_repo}")
    typer.echo("")
    for peer in peers_payload:
        _print_peer_summary(peer)
        typer.echo("")


@app.command()
def message(
    to_session_id: str = typer.Argument(..., help="Target session UUID."),
    text: str = typer.Argument(..., help="Message body."),
    from_session_id: str | None = typer.Option(
        None,
        "--from-session",
        "--from",
        help="Sender session UUID. Defaults to LONGHOUSE_SESSION_ID when available.",
    ),
    source_event_id: int | None = typer.Option(
        None,
        "--source-event-id",
        help="Optional source event id for traceability.",
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
    """Send a directed message to another session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_to_session_id = _parse_uuid_or_exit(to_session_id, label="to_session_id")
    resolved_from_session_id = _resolve_session_context(
        from_session_id or os.environ.get(_CURRENT_SESSION_ENV),
        label="from_session_id",
        guidance="Provide --from-session or run inside a managed session with LONGHOUSE_SESSION_ID set.",
    )

    body: dict[str, object] = {
        "to_session_id": resolved_to_session_id,
        "text": text,
    }
    if source_event_id is not None:
        body["source_event_id"] = source_event_id

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/messages",
                headers={
                    "X-Agents-Token": resolved_token,
                    _CURRENT_SESSION_HEADER: resolved_from_session_id,
                },
                json=body,
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
    if response.status_code not in (200, 201):
        detail = response.text[:200]
        try:
            payload = response.json()
            detail = str(payload.get("detail") or detail)
        except ValueError:
            pass
        typer.secho(f"API error: {response.status_code} {detail}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.secho("Message created.", fg=typer.colors.GREEN)
    typer.echo(f"Message ID: {payload.get('id')}")
    typer.echo(f"From: {resolved_from_session_id}")
    typer.echo(f"To: {resolved_to_session_id}")
    typer.echo(f"Status: {payload.get('delivery_status')}")
    delivered_via = str(payload.get("delivered_via") or "").strip()
    if delivered_via:
        typer.echo(f"Delivered via: {delivered_via}")


@app.command()
def tail(
    session_id: str = typer.Argument(..., help="Session UUID."),
    limit: int = typer.Option(
        30,
        "--limit",
        "-n",
        help="Number of recent events to return.",
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
    """Read the recent tail of a session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_session_id = _parse_uuid_or_exit(session_id, label="session_id")

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/sessions/{resolved_session_id}/tail",
                headers={"X-Agents-Token": resolved_token},
                params={"limit": limit},
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
        typer.secho(f"Session not found: {resolved_session_id}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    events = list(payload.get("events", []))
    if not events:
        typer.echo(f"No tail events found for session {resolved_session_id}")
        return

    typer.echo(f"Session: {resolved_session_id}")
    typer.echo(f"Events: {len(events)}")
    typer.echo("")
    for event in events:
        _print_tail_event(event)
        typer.echo("")


@app.command("check-messages")
def check_messages(
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session UUID. Defaults to LONGHOUSE_SESSION_ID when available.",
    ),
    direction: str = typer.Option(
        "inbound",
        "--direction",
        help="Message direction: inbound, outbound, or all.",
    ),
    unacknowledged_only: bool = typer.Option(
        True,
        "--unacknowledged-only/--all",
        help="Show only unacknowledged messages by default.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max messages to return.",
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
    """Inspect the durable message inbox for a session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_session_id = _resolve_session_context(
        session_id or os.environ.get(_CURRENT_SESSION_ENV),
        label="session_id",
        guidance="Provide --session or run inside a managed session with LONGHOUSE_SESSION_ID set.",
    )

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/messages",
                headers={
                    "X-Agents-Token": resolved_token,
                    _CURRENT_SESSION_HEADER: resolved_session_id,
                },
                params={
                    "direction": direction,
                    "unacknowledged_only": unacknowledged_only,
                    "limit": limit,
                },
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
    if response.status_code != 200:
        detail = response.text[:200]
        try:
            payload = response.json()
            detail = str(payload.get("detail") or detail)
        except ValueError:
            pass
        typer.secho(f"API error: {response.status_code} {detail}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    messages = list(payload.get("messages", []))
    if not messages:
        typer.echo(f"No messages found for session {resolved_session_id}")
        return

    typer.echo(f"Session: {resolved_session_id}")
    typer.echo(f"Messages: {len(messages)}")
    typer.echo("")
    for item in messages:
        _print_message_summary(item)
        typer.echo("")


@app.command("ack-message")
def ack_message(
    message_id: int = typer.Argument(..., help="Message id to acknowledge."),
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Target session UUID. Defaults to LONGHOUSE_SESSION_ID when available.",
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
    """Acknowledge an inbound message for the target session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_session_id = _resolve_session_context(
        session_id or os.environ.get(_CURRENT_SESSION_ENV),
        label="session_id",
        guidance="Provide --session or run inside a managed session with LONGHOUSE_SESSION_ID set.",
    )

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/messages/{message_id}/ack",
                headers={
                    "X-Agents-Token": resolved_token,
                    _CURRENT_SESSION_HEADER: resolved_session_id,
                },
                json={},
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
    if response.status_code != 200:
        detail = response.text[:200]
        try:
            payload = response.json()
            detail = str(payload.get("detail") or detail)
        except ValueError:
            pass
        typer.secho(f"API error: {response.status_code} {detail}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.secho("Message acknowledged.", fg=typer.colors.GREEN)
    typer.echo(f"Message ID: {payload.get('id')}")
    typer.echo(f"Status: {payload.get('delivery_status')}")
    acknowledged_at = str(payload.get("acknowledged_at") or "").strip()
    if acknowledged_at:
        typer.echo(f"Acknowledged at: {acknowledged_at}")
