"""CLI commands for session inspection primitives."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer

from zerg.cli._common import load_api_credentials
from zerg.cli._common import parse_uuid_or_exit
from zerg.services.managed_session_env import CURRENT_SESSION_HEADER
from zerg.services.managed_session_env import get_managed_session_id
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token

app = typer.Typer(help="Session inspection commands")


def _load_api_credentials(*, url: str | None, token: str | None, config_dir: Path | None) -> tuple[str, str]:
    return load_api_credentials(
        url=url,
        token=token,
        config_dir=config_dir,
        resolve_url=get_zerg_url,
        resolve_token=load_token,
    )


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


def _print_branch_stream(response: httpx.Response) -> int:
    saw_text = False
    exit_code = 0

    event_name: str | None = None
    data_lines: list[str] = []

    def _flush_event() -> int | None:
        nonlocal saw_text, exit_code, event_name, data_lines
        if event_name is None:
            data_lines = []
            return None

        raw_data = "\n".join(data_lines)
        payload: dict[str, object] = {}
        if raw_data:
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                payload = {"raw": raw_data}

        if event_name == "assistant_delta":
            text = str(payload.get("text") or "")
            if text:
                typer.echo(text, nl=False)
                saw_text = True
        elif event_name == "tool_use":
            if saw_text:
                typer.echo("")
                saw_text = False
            tool_name = str(payload.get("name") or "tool")
            typer.secho(f"[tool] {tool_name}", fg=typer.colors.YELLOW)
        elif event_name == "error":
            if saw_text:
                typer.echo("")
                saw_text = False
            typer.secho(str(payload.get("error") or raw_data or "Request failed"), fg=typer.colors.RED)
            exit_code = 1
        elif event_name == "done":
            if saw_text:
                typer.echo("")
                saw_text = False
            if payload.get("persistence_error"):
                typer.secho(str(payload["persistence_error"]), fg=typer.colors.YELLOW)
            if int(payload.get("exit_code") or 0) != 0:
                exit_code = 1

        event_name = None
        data_lines = []
        return None

    for raw_line in response.iter_lines():
        line = raw_line.decode() if isinstance(raw_line, bytes) else str(raw_line)
        if not line:
            _flush_event()
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())

    _flush_event()
    if saw_text:
        typer.echo("")
    return exit_code


def _should_use_live_send(session_payload: dict[str, object]) -> bool:
    capabilities = session_payload.get("capabilities")
    return bool(capabilities.get("live_control_available")) if isinstance(capabilities, dict) else False


def _format_api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if not isinstance(payload, dict):
        return response.text[:300]
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("error") or response.text[:300])
        exit_code = detail.get("exit_code")
        released_lock = detail.get("released_lock")
        parts = [message]
        if exit_code is not None:
            parts.append(f"exit_code: {exit_code}")
        if released_lock is not None:
            parts.append(f"released_lock: {str(bool(released_lock)).lower()}")
        return "  ".join(parts)
    if isinstance(detail, str):
        return detail[:300]
    return response.text[:300]


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


@app.command(name="continue")
def continue_session(
    session_id: str = typer.Argument(..., help="Session UUID to continue."),
    message: str = typer.Argument(..., help="Follow-up message."),
    current_session_id: str | None = typer.Option(
        None,
        "--current-session",
        help="Current session UUID. Defaults to the current managed session when available.",
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
    """Continue live work through the canonical machine-facing route."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_session_id = parse_uuid_or_exit(session_id, label="session_id")

    headers = {"X-Agents-Token": resolved_token}
    resolved_current_session_id = (current_session_id or get_managed_session_id() or "").strip()
    if resolved_current_session_id:
        headers[CURRENT_SESSION_HEADER] = parse_uuid_or_exit(
            resolved_current_session_id,
            label="current_session_id",
        )

    try:
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "POST",
                f"{base_url.rstrip('/')}/api/agents/sessions/{resolved_session_id}/send-live",
                headers=headers,
                json={"message": message},
            ) as response:
                if response.status_code == 401:
                    typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
                    raise typer.Exit(code=1)
                if response.status_code == 404:
                    typer.secho(f"Session not found: {resolved_session_id}", fg=typer.colors.RED)
                    raise typer.Exit(code=1)
                if response.status_code != 200:
                    detail = response.read().decode(errors="replace")[:200]
                    typer.secho(f"API error: {response.status_code} {detail}", fg=typer.colors.RED)
                    raise typer.Exit(code=1)

                content_type = str(response.headers.get("content-type") or "")
                if content_type.startswith("application/json"):
                    response.read()
                    payload = response.json()
                    if payload.get("accepted"):
                        typer.secho(
                            f"Accepted by session {payload.get('session_id')}",
                            fg=typer.colors.CYAN,
                            bold=True,
                        )
                        dispatch_ms = payload.get("dispatch_ms")
                        if dispatch_ms is not None:
                            typer.echo(f"dispatch_ms: {dispatch_ms}")
                        return

                    typer.secho(json.dumps(payload, indent=2), fg=typer.colors.RED)
                    raise typer.Exit(code=1)

                exit_code = _print_branch_stream(response)
                if exit_code:
                    raise typer.Exit(code=exit_code)
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {base_url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        typer.secho(f"Request timed out connecting to {base_url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def interrupt(
    session_id: str = typer.Argument(..., help="Managed-local session UUID to interrupt."),
    current_session_id: str | None = typer.Option(
        None,
        "--current-session",
        help="Current session UUID. Defaults to the current managed session when available.",
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
    """Interrupt the active turn in a managed-local session."""
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_session_id = parse_uuid_or_exit(session_id, label="session_id")

    headers = {"X-Agents-Token": resolved_token}
    resolved_current_session_id = (current_session_id or get_managed_session_id() or "").strip()
    if resolved_current_session_id:
        headers[CURRENT_SESSION_HEADER] = parse_uuid_or_exit(
            resolved_current_session_id,
            label="current_session_id",
        )

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/sessions/{resolved_session_id}/interrupt-live",
                headers=headers,
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
        typer.secho(f"API error: {response.status_code} {_format_api_error(response)}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if payload.get("interrupt_dispatched"):
        typer.secho(
            f"Interrupt request dispatched to session {payload.get('session_id') or resolved_session_id}",
            fg=typer.colors.CYAN,
        )
        if payload.get("confirmed_stopped") is False:
            typer.echo("confirmed_stopped: false")
        if payload.get("released_lock"):
            typer.echo("released_lock: true")
        return

    typer.secho(json.dumps(payload, indent=2), fg=typer.colors.RED)
    raise typer.Exit(code=1)
