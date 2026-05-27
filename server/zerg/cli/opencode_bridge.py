"""OpenCode bridge commands — drive the upstream OpenCode HTTP API.

Longhouse owns ``opencode serve`` (see ``zerg.cli.opencode``) and writes a
bridge state file that captures the live URL + Basic-Auth password. This
CLI is the corresponding seam consumed by the managed-local transport
(see ``zerg.services.managed_local_transport``) so browser / iOS / MCP
send-text / interrupt / steer flows can drive a managed OpenCode session
without re-discovery.

Endpoints used (from the upstream ``opencode`` server):

- ``POST /session``                          — create a session
- ``GET /session``                           — list sessions
- ``POST /session/{id}/message``             — send a message
- ``POST /session/{id}/abort``               — interrupt the active turn
- ``POST /permission/{request_id}/reply``    — reply to a permission prompt

The bridge always uses HTTP Basic auth with the username + password
captured in the bridge state file.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import httpx
import typer

from zerg.services.opencode_bridge_state import build_opencode_bridge_state_file
from zerg.services.opencode_bridge_state import read_opencode_bridge_state
from zerg.services.opencode_bridge_state import wait_for_opencode_bridge_state

app = typer.Typer(help="OpenCode bridge commands (managed sessions)")

_DEFAULT_HTTP_TIMEOUT_SECS = 10.0
_DEFAULT_WAIT_SECS = 10.0
_STEER_IDLE_POLL_SECS = 0.2
_STEER_IDLE_TIMEOUT_SECS = 5.0


class _BridgeError(Exception):
    """Raised when the bridge cannot reach a usable OpenCode server."""


def _client_from_state(state: dict[str, Any]) -> tuple[httpx.Client, str]:
    server_url = str(state.get("server_url") or "").strip().rstrip("/")
    server_username = str(state.get("server_username") or "opencode")
    server_password = str(state.get("server_password") or "")
    if not server_url:
        raise _BridgeError("OpenCode bridge state is missing server_url")
    if not server_password:
        raise _BridgeError("OpenCode bridge state is missing server_password")
    client = httpx.Client(
        base_url=server_url,
        auth=(server_username, server_password),
        timeout=_DEFAULT_HTTP_TIMEOUT_SECS,
    )
    return client, server_url


def _resolve_state(
    *,
    session_id: str,
    state_root: Path | None,
    config_dir: Path | None,
    wait_secs: float,
) -> dict[str, Any]:
    try:
        if wait_secs > 0:
            return wait_for_opencode_bridge_state(
                session_id=session_id,
                timeout_secs=wait_secs,
                state_root=state_root,
                config_dir=config_dir,
            )
        return read_opencode_bridge_state(
            session_id=session_id,
            state_root=state_root,
            config_dir=config_dir,
        )
    except FileNotFoundError as exc:
        raise _BridgeError(
            f"No OpenCode bridge state for session {session_id}. " "Is `longhouse opencode --cwd ... -- serve ...` running?"
        ) from exc


def _session_sort_timestamp(item: dict[str, Any]) -> float:
    """Best-effort newest-first ordering across opencode payload shapes.

    Newer opencode builds embed timestamps under ``time.{updated,created}``
    (numeric ms epoch). Older builds expose top-level ``updated``/``created``
    or ISO strings. Walk all known fields, parse numeric and ISO-8601 forms,
    and fall back to 0.0 when nothing parses — the enumerate index breaks
    ties deterministically at the call site.
    """

    candidates: list[Any] = []
    time_obj = item.get("time")
    if isinstance(time_obj, dict):
        for key in ("updated", "updatedAt", "modified", "created", "createdAt"):
            if key in time_obj:
                candidates.append(time_obj[key])
    for key in ("updated", "updatedAt", "modified", "created", "createdAt"):
        if key in item:
            candidates.append(item[key])
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            try:
                return float(stripped)
            except ValueError:
                pass
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return 0.0


def _list_sessions(client: httpx.Client) -> list[dict[str, Any]]:
    resp = client.get("/session")
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _resolve_target_session_id(
    client: httpx.Client,
    *,
    explicit: str | None,
    fallback: str | None,
    create_if_missing: bool,
) -> str:
    candidate = (explicit or "").strip() or (fallback or "").strip()
    if candidate:
        return candidate
    sessions = _list_sessions(client)
    if sessions:
        ordered = sorted(
            enumerate(sessions),
            key=lambda pair: (_session_sort_timestamp(pair[1]), pair[0]),
            reverse=True,
        )
        sid = str(ordered[0][1].get("id") or "").strip()
        if sid:
            return sid
    if not create_if_missing:
        raise _BridgeError("OpenCode server has no sessions yet")
    resp = client.post("/session", json={})
    resp.raise_for_status()
    payload = resp.json() if resp.content else {}
    if not isinstance(payload, dict):
        raise _BridgeError("Unexpected /session response shape")
    sid = str(payload.get("id") or "").strip()
    if not sid:
        raise _BridgeError("OpenCode created a session but returned no id")
    return sid


def _post_message(
    client: httpx.Client,
    *,
    opencode_session_id: str,
    text: str,
) -> dict[str, Any]:
    body = {
        "parts": [
            {"type": "text", "text": text},
        ],
    }
    resp = client.post(f"/session/{opencode_session_id}/message", json=body)
    if resp.status_code == 409:
        raise _BridgeError("OpenCode session is busy; interrupt or steer instead")
    resp.raise_for_status()
    if not resp.content:
        return {}
    payload = resp.json()
    return payload if isinstance(payload, dict) else {"result": payload}


def _abort_session(client: httpx.Client, *, opencode_session_id: str) -> None:
    resp = client.post(f"/session/{opencode_session_id}/abort", json={})
    if resp.status_code in (200, 204):
        return
    if resp.status_code == 404:
        # Already idle / cleaned up — treat as no-op.
        return
    resp.raise_for_status()


def _wait_until_idle(
    client: httpx.Client,
    *,
    opencode_session_id: str,
    timeout_secs: float,
) -> bool:
    """Poll ``GET /session`` until the target session reports a non-busy state."""

    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        sessions = _list_sessions(client)
        match = next(
            (item for item in sessions if str(item.get("id") or "") == opencode_session_id),
            None,
        )
        if match is None:
            return True
        # OpenCode marks busy via the in-flight assistant message; the
        # session listing exposes a transient "busy" / state field on
        # newer builds, plus a "time" object. Be lenient about both.
        busy = bool(match.get("busy"))
        state = str(match.get("state") or "").strip().lower()
        if not busy and state not in ("busy", "running"):
            return True
        time.sleep(_STEER_IDLE_POLL_SECS)
    return False


def _format_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"OpenCode HTTP {exc.response.status_code}: {exc.response.text.strip() or exc!r}"
    return str(exc)


@app.command("send")
def send(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    text: str = typer.Option(..., "--text", help="Text to send into OpenCode."),
    opencode_session_id: str | None = typer.Option(
        None,
        "--opencode-session-id",
        help="Override the OpenCode session id (otherwise resolved from state / listing).",
    ),
    state_root: Path | None = typer.Option(
        None,
        "--state-root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    config_dir: Path | None = typer.Option(
        None,
        "--config-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    wait_secs: float = typer.Option(_DEFAULT_WAIT_SECS, "--wait-secs", min=0.0),
) -> None:
    """Send a text message to the active managed OpenCode session."""

    try:
        state = _resolve_state(
            session_id=session_id,
            state_root=state_root,
            config_dir=config_dir,
            wait_secs=wait_secs,
        )
        client, _ = _client_from_state(state)
        with client:
            target = _resolve_target_session_id(
                client,
                explicit=opencode_session_id,
                fallback=str(state.get("opencode_session_id") or ""),
                create_if_missing=True,
            )
            _post_message(client, opencode_session_id=target, text=text)
    except (_BridgeError, httpx.HTTPError) as exc:
        typer.secho(_format_error(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command("interrupt")
def interrupt(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    opencode_session_id: str | None = typer.Option(None, "--opencode-session-id"),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    config_dir: Path | None = typer.Option(None, "--config-dir", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_WAIT_SECS, "--wait-secs", min=0.0),
    fallback_signal: bool = typer.Option(
        True,
        "--fallback-signal/--no-fallback-signal",
        help="If the HTTP abort fails, fall back to SIGINT on the captured opencode pid.",
    ),
) -> None:
    """Interrupt the active OpenCode turn (POST /session/{id}/abort)."""

    try:
        state = _resolve_state(
            session_id=session_id,
            state_root=state_root,
            config_dir=config_dir,
            wait_secs=wait_secs,
        )
        client, _ = _client_from_state(state)
        with client:
            try:
                target = _resolve_target_session_id(
                    client,
                    explicit=opencode_session_id,
                    fallback=str(state.get("opencode_session_id") or ""),
                    create_if_missing=False,
                )
            except _BridgeError:
                target = ""
            if target:
                try:
                    _abort_session(client, opencode_session_id=target)
                    return
                except httpx.HTTPError as exc:
                    if not fallback_signal:
                        raise
                    typer.secho(
                        f"OpenCode abort failed ({_format_error(exc)}); falling back to SIGINT.",
                        fg=typer.colors.YELLOW,
                        err=True,
                    )
        if not fallback_signal:
            raise _BridgeError("No active OpenCode session to abort")
        pid = int(state.get("opencode_pid") or 0)
        if pid <= 0:
            raise _BridgeError("Bridge state has no opencode_pid for fallback SIGINT")
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError as exc:
            raise _BridgeError(f"OpenCode pid {pid} is not running") from exc
    except (_BridgeError, httpx.HTTPError) as exc:
        typer.secho(_format_error(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command("steer")
def steer(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    text: str = typer.Option(..., "--text"),
    opencode_session_id: str | None = typer.Option(None, "--opencode-session-id"),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    config_dir: Path | None = typer.Option(None, "--config-dir", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_WAIT_SECS, "--wait-secs", min=0.0),
    idle_timeout_secs: float = typer.Option(_STEER_IDLE_TIMEOUT_SECS, "--idle-timeout-secs", min=0.0),
) -> None:
    """Mid-turn steer: abort the running turn, wait for idle, send new text."""

    try:
        state = _resolve_state(
            session_id=session_id,
            state_root=state_root,
            config_dir=config_dir,
            wait_secs=wait_secs,
        )
        client, _ = _client_from_state(state)
        with client:
            target = _resolve_target_session_id(
                client,
                explicit=opencode_session_id,
                fallback=str(state.get("opencode_session_id") or ""),
                create_if_missing=True,
            )
            try:
                _abort_session(client, opencode_session_id=target)
            except httpx.HTTPError as exc:
                typer.secho(
                    f"OpenCode abort during steer failed ({_format_error(exc)}); proceeding anyway.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
            _wait_until_idle(
                client,
                opencode_session_id=target,
                timeout_secs=idle_timeout_secs,
            )
            _post_message(client, opencode_session_id=target, text=text)
    except (_BridgeError, httpx.HTTPError) as exc:
        typer.secho(_format_error(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command("permission-reply")
def permission_reply(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    request_id: str = typer.Option(..., "--request-id", help="OpenCode permission request id."),
    decision: str = typer.Option(..., "--decision", help="`allow`, `deny`, or `always`."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    config_dir: Path | None = typer.Option(None, "--config-dir", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_WAIT_SECS, "--wait-secs", min=0.0),
) -> None:
    """Reply to an OpenCode permission prompt."""

    decision_normalized = decision.strip().lower()
    if decision_normalized not in {"allow", "deny", "always"}:
        raise typer.BadParameter("decision must be one of: allow, deny, always")
    try:
        state = _resolve_state(
            session_id=session_id,
            state_root=state_root,
            config_dir=config_dir,
            wait_secs=wait_secs,
        )
        client, _ = _client_from_state(state)
        with client:
            resp = client.post(
                f"/permission/{request_id}/reply",
                json={"decision": decision_normalized},
            )
            if resp.status_code not in (200, 204):
                resp.raise_for_status()
    except (_BridgeError, httpx.HTTPError) as exc:
        typer.secho(_format_error(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command("inspect")
def inspect_state(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    config_dir: Path | None = typer.Option(None, "--config-dir", file_okay=False, dir_okay=True, resolve_path=True),
    redact_password: bool = typer.Option(
        True,
        "--redact-password/--no-redact-password",
        help="Replace server_password with '<redacted>' before printing.",
    ),
) -> None:
    """Print the current OpenCode bridge state JSON."""

    state_path = build_opencode_bridge_state_file(
        session_id=session_id,
        state_root=state_root,
        config_dir=config_dir,
    )
    if not state_path.exists():
        typer.secho(f"No OpenCode bridge state at {state_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    payload = read_opencode_bridge_state(
        session_id=session_id,
        state_root=state_root,
        config_dir=config_dir,
    )
    if redact_password and "server_password" in payload:
        payload = {**payload, "server_password": "<redacted>"}
    typer.echo(json.dumps(payload, indent=2))


__all__ = ["app"]
