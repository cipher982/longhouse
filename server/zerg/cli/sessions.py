"""CLI commands for session inspection primitives."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
import typer

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.cli._common import load_api_credentials
from zerg.cli._common import parse_uuid_or_exit
from zerg.services.catalogd_supervisor import catalogd_paths
from zerg.services.managed_session_env import CURRENT_SESSION_HEADER
from zerg.services.managed_session_env import get_managed_session_id
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token

app = typer.Typer(help="Session inspection commands")


@app.command("repair-codex-launch-visibility")
def repair_codex_launch_visibility(
    session_id: str = typer.Argument(..., help="Exact Codex session UUID."),
    apply: bool = typer.Option(False, "--apply", help="Apply the dry-run plan."),
    expected_fingerprint: str | None = typer.Option(
        None,
        "--expected-fingerprint",
        help="Exact fingerprint printed by a preceding dry-run.",
    ),
) -> None:
    """Repair one unambiguous sticky-hidden Codex Helm on this Runtime Host."""

    canonical_session_id = parse_uuid_or_exit(session_id, label="session_id")
    if apply and expected_fingerprint is None:
        typer.secho("--apply requires --expected-fingerprint from dry-run", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if not apply and expected_fingerprint is not None:
        typer.secho("dry-run does not accept --expected-fingerprint", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    _, socket_path = catalogd_paths()

    async def execute() -> dict[str, object]:
        client = CatalogClient(socket_path)
        try:
            return await client.call(
                "session.repair.codex_launch_visibility.v2",
                {
                    "session_id": canonical_session_id,
                    "dry_run": not apply,
                    "expected_fingerprint": expected_fingerprint,
                },
            )
        finally:
            await client.close()

    try:
        result = asyncio.run(execute())
    except CatalogRemoteError as exc:
        typer.secho(f"catalogd rejected repair: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except (CatalogUnavailable, OSError) as exc:
        typer.secho(f"catalogd is unavailable at {socket_path}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    if result.get("eligible") is not True or (apply and result.get("applied") is not True):
        raise typer.Exit(code=2)


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


def _parse_retry_after(response: httpx.Response, default: float = 1.0, max_delay: float = 5.0) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(0.5, min(float(header), max_delay))
        except ValueError:
            pass
    return default


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


@app.command(name="list")
def list_sessions(
    project: str | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Filter by project name.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Filter by provider (omp, claude, codex, opencode, cursor).",
    ),
    device: str | None = typer.Option(
        None,
        "--device",
        help="Filter by machine/device id.",
    ),
    days_back: int = typer.Option(
        14,
        "--days-back",
        "-d",
        help="How many days back to look.",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        "-q",
        help="Text query over session content.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        "-n",
        help="Max sessions to return.",
    ),
    offset: int = typer.Option(
        0,
        "--offset",
        help="Offset into the result list.",
    ),
    include_automation: bool = typer.Option(
        False,
        "--include-automation",
        help="Include automation-launched sessions, which are hidden by default.",
    ),
    include_test: bool = typer.Option(
        False,
        "--include-test",
        help="Include test and proof sessions.",
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
    """List recent sessions, newest activity first.

    This is the entry point when you do not already know a session id: it
    answers "what ran lately on this machine, or in this project" without
    requiring a text query. Rows carry machine-readable activity and launch
    provenance; they deliberately do not assert whether a session has ended,
    because the machine surface carries no closure fact.
    """
    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)

    params: dict[str, object] = {
        "days_back": days_back,
        "limit": limit,
        "offset": offset,
        "include_automation": include_automation,
        "include_test": include_test,
    }
    if project:
        params["project"] = project
    if provider:
        params["provider"] = provider
    if device:
        params["device_id"] = device
    if query:
        params["query"] = query

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/sessions",
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
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = response.json()
    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    sessions = [row for row in payload.get("sessions", []) if isinstance(row, dict)]
    if not sessions:
        typer.echo("No sessions found.")
        return

    typer.echo(f"Sessions: {len(sessions)} of {payload.get('total', len(sessions))}")
    typer.echo("")
    for row in sessions:
        typer.secho(str(row.get("id") or "-"), fg=typer.colors.CYAN, bold=True)
        typer.echo(
            "  {provider}  {project}  {device}  last activity {last}".format(
                provider=row.get("provider") or "-",
                project=row.get("project") or "-",
                device=row.get("device_id") or "-",
                last=row.get("last_activity_at") or "-",
            )
        )
        title = str(row.get("title") or "").strip()
        if title:
            typer.echo(f"  {title}")
        typer.echo("")


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
    # The machine surface serves an archival projection: it carries no closure
    # fact, and its `ended_at` mirrors last event time rather than a process
    # exit. Reporting "ended" from that field claimed something this view cannot
    # observe -- a session whose process is still attached, and one that died
    # without committing a terminal, both read as "ended". Report the fact that
    # is actually present.
    typer.echo(
        "  provider: {provider}  project: {project}  last activity: {last}".format(
            provider=payload.get("provider") or "-",
            project=payload.get("project") or "-",
            last=payload.get("last_activity_at") or "-",
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

    provider_session_id = str(payload.get("provider_session_id") or "").strip()
    if provider_session_id:
        typer.echo(f"  provider session: {provider_session_id}")

    title = str(payload.get("title") or "").strip()
    if title:
        typer.echo(f"  title: {title}")

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
        help="Offset into the event list (legacy hosts; storage-v2 hosts page with --cursor).",
    ),
    cursor: str | None = typer.Option(
        None,
        "--cursor",
        help="Resume after this cursor: the previous page's next_cursor.",
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
    }
    # Storage-v2 hosts reject a nonzero offset and page by cursor instead.
    if cursor:
        params["cursor"] = cursor
    elif offset:
        params["offset"] = offset
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
    steer: bool = typer.Option(
        False,
        "--steer",
        help=(
            "Enter the target's running turn instead of queueing for its next turn boundary. "
            "Best effort: the provider applies it at its next boundary, and an idle target is refused."
        ),
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output the raw response.",
    ),
    client_request_id: str | None = typer.Option(
        None,
        "--client-request-id",
        help=(
            "Idempotency key for this message. Defaults to a fresh id. Reuse the id a timed-out "
            "run printed to resolve it without sending the text twice."
        ),
    ),
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
    """Send a peer a message with an explicit delivery semantic.

    Two semantics exist and they are not interchangeable:

    * default (SEND): durable and ordered. The message is committed to the
      target's input queue and the target takes it at its next turn boundary.
      A target that is mid-turn waits for its current turn to end, and the
      message survives the wait.
    * ``--steer``: immediate and best effort. The text enters the turn the
      target is running right now, which can change what that turn does. The
      provider decides exactly when it lands (for example after the current
      tool call), and a target that is not running a turn is refused rather
      than silently upgraded to a new one.

    Both report the receipt the delivery was recorded under; read the target
    transcript to confirm the model received it.
    """

    config_dir = Path(claude_dir) if claude_dir else None
    base_url, resolved_token = _load_api_credentials(url=url, token=token, config_dir=config_dir)
    resolved_session_id = parse_uuid_or_exit(session_id, label="session_id")
    intent = "steer" if steer else "queue"
    resolved_client_request_id = (client_request_id or "").strip() or uuid.uuid4().hex

    headers = {"X-Agents-Token": resolved_token}
    resolved_current_session_id = (current_session_id or get_managed_session_id() or "").strip()
    if resolved_current_session_id:
        headers[CURRENT_SESSION_HEADER] = parse_uuid_or_exit(
            resolved_current_session_id,
            label="current_session_id",
        )

    input_url = f"{base_url.rstrip('/')}/api/agents/sessions/{resolved_session_id}/input"
    payload = {"text": message, "intent": intent, "client_request_id": resolved_client_request_id}
    max_429_retries = 3

    try:
        with httpx.Client(timeout=30) as client:
            response = None
            for attempt in range(max_429_retries + 1):
                response = client.post(input_url, headers=headers, json=payload)
                if response.status_code == 429 and attempt < max_429_retries:
                    time.sleep(_parse_retry_after(response))
                    continue
                break
            assert response is not None

            if response.status_code == 401:
                typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            if response.status_code == 404:
                typer.secho(f"Session not found: {resolved_session_id}", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            if response.status_code not in (200, 201):
                raise typer.Exit(
                    code=_report_continue_failure(
                        response,
                        session_id=resolved_session_id,
                        steer=steer,
                        client_request_id=resolved_client_request_id,
                    )
                )

            body = response.json()
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {base_url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        # The server may have committed the input before the answer was lost.
        # Printing the key is what makes a re-run a resolution, not a resend.
        typer.secho(f"Request to {base_url} timed out.", fg=typer.colors.YELLOW, bold=True)
        typer.echo(f"client_request_id: {resolved_client_request_id}")
        typer.echo("It may already have been delivered. Re-run with --client-request-id set to that value.")
        raise typer.Exit(code=1)

    if output_json:
        typer.echo(json.dumps(body, indent=2))
        return

    _report_continue_outcome(body, session_id=resolved_session_id, steer=steer)


def _report_continue_failure(
    response: httpx.Response,
    *,
    session_id: str,
    steer: bool,
    client_request_id: str,
) -> int:
    """Explain what the server refused, in the sender's terms."""

    detail = response.json().get("detail") if _looks_like_json(response) else None
    code = detail.get("error_code") if isinstance(detail, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else None
    if not isinstance(message, str) or not message:
        message = detail if isinstance(detail, str) and detail else response.text[:200]

    if response.status_code in {502, 503, 504} or code in {"delivery_unknown", "input_receipt_unknown"}:
        # The request may have been committed before the answer was lost. The
        # key is what lets the sender resolve that instead of sending twice.
        typer.secho(f"Delivery not confirmed: {message}", fg=typer.colors.YELLOW, bold=True)
        typer.echo(f"client_request_id: {client_request_id}")
        typer.echo("It may already have been delivered. Re-run with --client-request-id set to that value.")
        return 1

    if code in {"steer_requires_active_turn", "turn_ended"}:
        typer.secho(f"Not sent: {message}", fg=typer.colors.YELLOW, bold=True)
        if steer:
            typer.echo("The target is not running a turn. Re-run without --steer to queue it durably.")
        else:
            typer.echo("The target is not running a turn this steer could join.")
        return 1

    typer.secho(f"API error: {response.status_code} {message}", fg=typer.colors.RED)
    return 1


def _looks_like_json(response: httpx.Response) -> bool:
    return str(response.headers.get("content-type") or "").startswith("application/json")


def _report_continue_outcome(body: dict[str, object], *, session_id: str, steer: bool) -> None:
    """Say what happened to the message, never just that the transport answered."""

    receipt_id = body.get("live_input_id")
    outcome = str(body.get("outcome") or "unknown")

    if outcome == "unknown":
        # A replay of an in-flight delivery reports the state that is actually
        # known. It is not evidence the model received anything, and a steer
        # that has not been acknowledged must not read as one that was.
        typer.secho(f"Delivery outcome unknown for {session_id}.", fg=typer.colors.YELLOW, bold=True)
        typer.echo(f"Receipt: {receipt_id}")
        typer.echo("Read the target transcript before sending again: this message may already have landed.")
    elif steer:
        typer.secho(f"Steered into {session_id}'s running turn.", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"Receipt: {receipt_id}")
        typer.echo("Best effort: the provider applies this at its next boundary, not instantly.")
    elif outcome == "sent":
        typer.secho(f"Delivered to {session_id}.", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"Receipt: {receipt_id}")
    else:
        typer.secho(f"Queued for {session_id}.", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"Receipt: {receipt_id}")
        typer.echo(
            "Not delivered yet: the target takes it at its next turn boundary. "
            "A queued input expires after 30 minutes and is then reported failed."
        )

    typer.echo(f"Confirm: longhouse-server tail {session_id}")


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

    interrupt_url = f"{base_url.rstrip('/')}/api/agents/sessions/{resolved_session_id}/interrupt-live"
    max_429_retries = 3

    try:
        with httpx.Client(timeout=30) as client:
            for attempt in range(max_429_retries + 1):
                response = client.post(interrupt_url, headers=headers)
                if response.status_code == 429 and attempt < max_429_retries:
                    delay = _parse_retry_after(response)
                    time.sleep(delay)
                    continue
                break
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
