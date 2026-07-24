"""CLI commands for session coordination primitives."""

from __future__ import annotations

import json
import os
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
    typer.echo(f"  provider: {provider}  device: {device_name}  control: {control_label}  presence: {presence_state}  branch: {branch}")
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


def _print_directed_input(item: dict) -> None:
    input_id = str(item.get("id") or "-")
    source_session_id = str(item.get("source_session_id") or "-")
    created_at = str(item.get("created_at") or "-")
    receipt = item.get("input_receipt") if isinstance(item.get("input_receipt"), dict) else None
    receipt_status = str(receipt.get("status") or "-") if receipt else "not-attempted"
    text = str(item.get("text") or "").strip()

    typer.secho(f"#{input_id}  {receipt_status}  {created_at}", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  from: {source_session_id}")
    if text:
        typer.echo(f"  {text}")


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
                "cwd": item.get("cwd"),
                "git_repo": item.get("git_repo"),
                "git_branch": item.get("git_branch"),
                "summary_title": item.get("summary_title"),
                "presence_state": item.get("presence_state"),
                "kernel_control_label": item.get("kernel_control_label"),
                "kernel_live_control_available": item.get("kernel_live_control_available"),
                "kernel_host_reattach_available": item.get("kernel_host_reattach_available"),
                "kernel_observe_only": item.get("kernel_observe_only"),
                "kernel_search_only": item.get("kernel_search_only"),
                "kernel_staleness_reason": item.get("kernel_staleness_reason"),
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


def send(
    session_id: str = typer.Argument(..., help="Target session UUID."),
    text: str = typer.Argument(..., help="Directed input body."),
    client_request_id: str | None = typer.Option(
        None,
        "--client-request-id",
        help="Optional idempotency key.",
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
    """Send durable attributed input to another managed session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, _resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    coordination_token = str(os.environ.get("LONGHOUSE_COORDINATION_TOKEN") or "").strip()
    if not coordination_token:
        typer.secho("send requires session-scoped coordination authority.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    resolved_target_session_id = parse_uuid_or_exit(session_id, label="session_id")
    resolved_source_session_id = _resolve_session_context(
        get_managed_session_id(),
        label="current_session_id",
        guidance="Run send inside a Longhouse-managed session.",
    )

    body: dict[str, object] = {
        "target_session_id": resolved_target_session_id,
        "text": text,
    }
    if client_request_id is not None:
        body["client_request_id"] = client_request_id

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/directed-inputs",
                headers={
                    "X-Agents-Token": coordination_token,
                    CURRENT_SESSION_HEADER: resolved_source_session_id,
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

    typer.secho("Directed input created.", fg=typer.colors.GREEN)
    typer.echo(f"Input ID: {payload.get('id')}")
    typer.echo(f"From: {resolved_source_session_id}")
    typer.echo(f"To: {resolved_target_session_id}")
    receipt = payload.get("input_receipt") if isinstance(payload.get("input_receipt"), dict) else None
    typer.echo(f"Provider input: {receipt.get('status') if receipt else 'not attempted'}")


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


def inbox(
    direction: str = typer.Option(
        "inbound",
        "--direction",
        help="Input direction: inbound, outbound, or all.",
    ),
    after_cursor: int = typer.Option(
        0,
        "--after-cursor",
        min=0,
        help="Return inputs after this stable id cursor.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max inputs to return.",
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
    """Recover durable input for the current managed session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, _resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    coordination_token = str(os.environ.get("LONGHOUSE_COORDINATION_TOKEN") or "").strip()
    if not coordination_token:
        typer.secho("inbox requires session-scoped coordination authority.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    resolved_session_id = _resolve_session_context(
        get_managed_session_id(),
        label="session_id",
        guidance="Run inbox inside a Longhouse-managed session.",
    )

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/directed-inputs",
                headers={
                    "X-Agents-Token": coordination_token,
                    CURRENT_SESSION_HEADER: resolved_session_id,
                },
                params={
                    "direction": direction,
                    "after_id": after_cursor,
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

    directed_inputs = list(payload.get("directed_inputs", []))
    if not directed_inputs:
        typer.echo(f"No directed input found for session {resolved_session_id}")
        return

    typer.echo(f"Session: {resolved_session_id}")
    typer.echo(f"Inputs: {len(directed_inputs)}")
    typer.echo(f"Next cursor: {payload.get('next_cursor', after_cursor)}")
    typer.echo("")
    for item in directed_inputs:
        _print_directed_input(item)
        typer.echo("")


def reply(
    input_id: int = typer.Argument(..., help="Inbound directed input id."),
    text: str = typer.Argument(..., help="Reply body."),
    client_request_id: str | None = typer.Option(
        None,
        "--client-request-id",
        help="Optional idempotency key.",
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
    """Reply to inbound input without copying its source session id."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, _resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    coordination_token = str(os.environ.get("LONGHOUSE_COORDINATION_TOKEN") or "").strip()
    if not coordination_token:
        typer.secho("reply requires session-scoped coordination authority.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    resolved_session_id = _resolve_session_context(
        get_managed_session_id(),
        label="session_id",
        guidance="Run reply inside a Longhouse-managed session.",
    )

    body: dict[str, str] = {"text": text}
    if client_request_id is not None:
        body["client_request_id"] = client_request_id

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/directed-inputs/{input_id}/reply",
                headers={
                    "X-Agents-Token": coordination_token,
                    CURRENT_SESSION_HEADER: resolved_session_id,
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

    typer.secho("Reply created.", fg=typer.colors.GREEN)
    typer.echo(f"Input ID: {payload.get('id')}")
    typer.echo(f"Reply to: {payload.get('reply_to_id')}")
