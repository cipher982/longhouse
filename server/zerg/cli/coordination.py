"""CLI commands for session coordination primitives."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx
import typer

from zerg.cli._common import git_output
from zerg.cli._common import load_api_credentials
from zerg.cli._common import parse_uuid_or_exit
from zerg.services.managed_session_env import CURRENT_SESSION_HEADER
from zerg.services.managed_session_env import get_managed_session_id
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token

messages_app = typer.Typer(help="Durable session inbox commands")


def _load_api_credentials(*, url: str | None, token: str | None, config_dir: Path | None) -> tuple[str, str]:
    return load_api_credentials(
        url=url,
        token=token,
        config_dir=config_dir,
        resolve_url=get_zerg_url,
        resolve_token=load_token,
    )


def _resolve_repo_context(*, explicit_repo: str | None, base_url: str, token: str) -> tuple[str | None, str | None]:
    if explicit_repo:
        return explicit_repo.strip(), None

    current_session_id = ""
    env_session_id = str(get_managed_session_id() or "")
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
    local_repo = git_output(cwd, "rev-parse", "--show-toplevel")
    if local_repo:
        return local_repo, current_session_id or None

    remote_repo = git_output(cwd, "config", "--get", "remote.origin.url")
    if remote_repo:
        return remote_repo, current_session_id or None

    return None, current_session_id or None


def _print_peer_summary(peer: dict) -> None:
    session_id = str(peer.get("session_id") or "")
    provider = str(peer.get("provider") or "-")
    device_name = str(peer.get("device_name") or "-")
    control_label = str(peer.get("kernel_control_label") or peer.get("control_label") or "-")
    presence_state = str(peer.get("presence_state") or "-")
    branch = str(peer.get("git_branch") or "-")
    title = str(peer.get("summary_title") or "").strip()

    typer.secho(session_id, fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  provider: {provider}  device: {device_name}  " f"control: {control_label}  presence: {presence_state}  branch: {branch}")
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
    return parse_uuid_or_exit(raw, label=label, missing_message=guidance)


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


def _wall_value(item: dict, kernel_key: str, legacy_key: str):
    if kernel_key in item:
        return item.get(kernel_key)
    return item.get(legacy_key)


def _fetch_wall_payload(
    *,
    base_url: str,
    token: str,
    repo: str | None = None,
    project: str | None = None,
    days: int,
    limit: int,
) -> dict:
    params: dict[str, object] = {"days": days, "limit": limit}
    if repo:
        params["repo"] = repo
    if project:
        params["project"] = project

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/sessions/wall",
                headers={"X-Agents-Token": token},
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
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    return response.json()


def _print_wall_session_summary(session: dict) -> None:
    session_id = str(session.get("session_id") or "")
    provider = str(session.get("provider") or "-")
    device_name = str(session.get("device_name") or "-")
    presence_state = str(session.get("presence_state") or "-")
    control_label = str(session.get("kernel_control_label") or session.get("control_label") or "-")
    branch = str(session.get("git_branch") or "-")
    repo = str(session.get("git_repo") or "-")
    last_event_at = str(session.get("last_event_at") or "-")
    title = str(session.get("summary_title") or "").strip()

    typer.secho(session_id, fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  provider: {provider}  device: {device_name}  " f"control: {control_label}  presence: {presence_state}  branch: {branch}")
    typer.echo(f"  repo: {repo}  last_event_at: {last_event_at}")
    if title:
        typer.echo(f"  {title}")


def wall(
    repo: str | None = typer.Option(
        None,
        "--repo",
        "-r",
        help="Optional repo filter (substring match on session git_repo).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Optional project filter.",
    ),
    days: int = typer.Option(
        7,
        "--days",
        help="Days to look back for session activity.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max wall sessions to return.",
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
    """Read the raw wall surface for recent sessions."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    payload = _fetch_wall_payload(
        base_url=base_url,
        token=resolved_token,
        repo=repo,
        project=project,
        days=days,
        limit=limit,
    )

    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    sessions = list(payload.get("sessions", []))
    if not sessions:
        typer.echo("No wall sessions found.")
        return

    typer.echo(f"Found {len(sessions)} wall session{'s' if len(sessions) != 1 else ''}")
    if repo:
        typer.echo(f"Repo filter: {repo}")
    if project:
        typer.echo(f"Project filter: {project}")
    typer.echo("")
    for session in sessions:
        _print_wall_session_summary(session)
        typer.echo("")


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
                "or inside a Longhouse-managed session.",
            ]
        )
        typer.secho(message, fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = _fetch_wall_payload(
        base_url=base_url,
        token=resolved_token,
        repo=resolved_repo,
        days=days,
        limit=limit,
    )
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
                "kernel_control_label": _wall_value(item, "kernel_control_label", "control_label"),
                "kernel_live_control_available": _wall_value(
                    item,
                    "kernel_live_control_available",
                    "live_control_available",
                ),
                "kernel_host_reattach_available": _wall_value(
                    item,
                    "kernel_host_reattach_available",
                    "host_reattach_available",
                ),
                "kernel_observe_only": _wall_value(item, "kernel_observe_only", "observe_only"),
                "kernel_search_only": _wall_value(item, "kernel_search_only", "search_only"),
                "kernel_staleness_reason": _wall_value(item, "kernel_staleness_reason", "staleness_reason"),
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


def message(
    to_session_id: str = typer.Argument(..., help="Target session UUID."),
    text: str = typer.Argument(..., help="Message body."),
    from_session_id: str | None = typer.Option(
        None,
        "--from-session",
        "--from",
        help="Sender session UUID. Defaults to the current managed session when available.",
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
    resolved_to_session_id = parse_uuid_or_exit(to_session_id, label="to_session_id")
    resolved_from_session_id = _resolve_session_context(
        from_session_id or get_managed_session_id(),
        label="from_session_id",
        guidance="Provide --from-session or run inside a Longhouse-managed session.",
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
                    CURRENT_SESSION_HEADER: resolved_from_session_id,
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
    resolved_session_id = parse_uuid_or_exit(session_id, label="session_id")

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


def check_messages(
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session UUID. Defaults to the current managed session when available.",
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
        session_id or get_managed_session_id(),
        label="session_id",
        guidance="Provide --session or run inside a Longhouse-managed session.",
    )

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/messages",
                headers={
                    "X-Agents-Token": resolved_token,
                    CURRENT_SESSION_HEADER: resolved_session_id,
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


def ack_message(
    message_id: int = typer.Argument(..., help="Message id to acknowledge."),
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Target session UUID. Defaults to the current managed session when available.",
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
        session_id or get_managed_session_id(),
        label="session_id",
        guidance="Provide --session or run inside a Longhouse-managed session.",
    )

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/messages/{message_id}/ack",
                headers={
                    "X-Agents-Token": resolved_token,
                    CURRENT_SESSION_HEADER: resolved_session_id,
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


@messages_app.callback(invoke_without_command=True)
def messages(
    ctx: typer.Context,
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session UUID. Defaults to the current managed session when available.",
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
    """List durable session messages for a session."""
    if ctx.invoked_subcommand is not None:
        return
    check_messages(
        session_id=session_id,
        direction=direction,
        unacknowledged_only=unacknowledged_only,
        limit=limit,
        output_json=output_json,
        url=url,
        token=token,
        claude_dir=claude_dir,
    )


@messages_app.command("ack")
def messages_ack(
    message_id: int = typer.Argument(..., help="Message id to acknowledge."),
    session_id: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Target session UUID. Defaults to the current managed session when available.",
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
    """Acknowledge an inbound durable session message."""
    ack_message(
        message_id=message_id,
        session_id=session_id,
        output_json=output_json,
        url=url,
        token=token,
        claude_dir=claude_dir,
    )
