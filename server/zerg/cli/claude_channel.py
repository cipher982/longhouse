"""Claude channel bridge commands for native managed-local Claude sessions."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp.types as mcp_types
import typer
from mcp.server.lowlevel.server import Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_SERVER_NAME
from zerg.services.claude_channel_bridge import build_claude_channel_state_file
from zerg.services.claude_channel_bridge import read_claude_channel_state
from zerg.services.claude_channel_bridge import resolve_claude_channel_state_root
from zerg.services.claude_channel_bridge import wait_for_claude_channel_state

app = typer.Typer(help="Claude channel bridge commands")

_CHANNEL_EXPERIMENTAL_CAPABILITIES = {"claude/channel": {}}
_CHANNEL_BRIDGE_NAME = CLAUDE_CHANNEL_SERVER_NAME
_DEFAULT_SEND_WAIT_SECS = 10.0
_DEFAULT_HTTP_TIMEOUT_SECS = 5.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_meta(meta_entries: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in meta_entries or []:
        raw = str(entry or "").strip()
        if not raw:
            continue
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            raise typer.BadParameter(f"meta entry must be key=value, got {entry!r}")
        parsed[key.strip()] = value
    return parsed


@dataclass
class _BridgeState:
    session_id: str | None
    provider_session_id: str | None
    state_root: Path
    auth_token: str
    port: int
    claude_pid: int | None
    bridge_pid: int
    ready: bool
    started_at: str

    def as_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "provider_session_id": self.provider_session_id,
            "state_root": str(self.state_root),
            "auth_token": self.auth_token,
            "port": self.port,
            "claude_pid": self.claude_pid,
            "bridge_pid": self.bridge_pid,
            "ready": self.ready,
            "started_at": self.started_at,
            "updated_at": _utc_now_iso(),
        }


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler], *, bridge):
        super().__init__(server_address, handler_cls)
        self.bridge = bridge


class _ChannelBridgeServer(Server[None, mcp_types.ServerRequest]):
    def __init__(
        self,
        *,
        session_id: str | None,
        provider_session_id: str | None,
        state_root: Path,
        port: int,
        auth_token: str,
        claude_pid: int | None,
    ) -> None:
        super().__init__(
            name=_CHANNEL_BRIDGE_NAME,
            instructions=("Longhouse native Claude channel bridge. " "Claude may receive channel notifications from this local server."),
        )
        self._managed_session_id = str(session_id or "").strip() or None
        self._provider_session_id = str(provider_session_id or "").strip() or None
        self._state_root = state_root
        self._requested_port = int(port)
        self._auth_token = auth_token
        self._claude_pid = claude_pid
        self._started_at = _utc_now_iso()
        self._session: ServerSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._initialized = asyncio.Event()
        self._http_server: _BridgeHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self.notification_handlers[mcp_types.InitializedNotification] = self._handle_initialized

    @property
    def state_file(self) -> Path | None:
        if not self._managed_session_id:
            return None
        return build_claude_channel_state_file(
            session_id=self._managed_session_id,
            state_root=self._state_root,
        )

    async def _handle_initialized(self, _notification: mcp_types.InitializedNotification) -> None:
        self._initialized.set()
        self._write_state()

    def _build_state(self) -> _BridgeState:
        port = int(self._http_server.server_address[1]) if self._http_server else self._requested_port
        return _BridgeState(
            session_id=self._managed_session_id,
            provider_session_id=self._provider_session_id,
            state_root=self._state_root,
            auth_token=self._auth_token,
            port=port,
            claude_pid=self._claude_pid,
            bridge_pid=os.getpid(),
            ready=self._initialized.is_set(),
            started_at=self._started_at,
        )

    def _write_state(self) -> None:
        state_path = self.state_file
        if state_path is None:
            return
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(self._build_state().as_json(), indent=2) + "\n", encoding="utf-8")

    def _remove_state(self) -> None:
        state_path = self.state_file
        if state_path is None:
            return
        try:
            state_path.unlink()
        except FileNotFoundError:
            return

    async def emit_channel(self, *, content: str, meta: dict[str, str] | None = None) -> None:
        text = str(content or "")
        if not text.strip():
            raise ValueError("content must not be empty")
        if self._managed_session_id:
            await self._initialized.wait()
        if self._session is None:
            raise RuntimeError("MCP session is not ready")
        payload = {"content": text}
        normalized_meta = {str(key): str(value) for key, value in (meta or {}).items() if str(key)}
        if normalized_meta:
            payload["meta"] = normalized_meta
        notification = mcp_types.JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params=payload,
        )
        await self._session._write_stream.send(  # type: ignore[attr-defined]
            SessionMessage(message=mcp_types.JSONRPCMessage(notification))
        )

    def emit_channel_from_thread(self, *, content: str, meta: dict[str, str] | None = None) -> None:
        if self._loop is None:
            raise RuntimeError("Channel bridge event loop is not ready")
        future = asyncio.run_coroutine_threadsafe(self.emit_channel(content=content, meta=meta), self._loop)
        future.result(timeout=_DEFAULT_HTTP_TIMEOUT_SECS)

    def _start_http_server(self) -> None:
        if not self._managed_session_id:
            return

        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, _fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/health":
                    self.send_error(404)
                    return
                self._send_json(bridge._build_state().as_json())

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/inject":
                    self.send_error(404)
                    return
                expected = bridge._auth_token
                provided = self.headers.get("X-Longhouse-Channel-Token", "")
                if expected and provided != expected:
                    self.send_error(403)
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self.send_error(400, "invalid json")
                    return
                content = str(payload.get("content") or "")
                meta = payload.get("meta")
                if meta is not None and not isinstance(meta, dict):
                    self.send_error(400, "meta must be an object")
                    return
                try:
                    bridge.emit_channel_from_thread(
                        content=content,
                        meta={str(key): str(value) for key, value in (meta or {}).items()},
                    )
                except Exception as exc:
                    self.send_error(500, str(exc))
                    return
                self.send_response(204)
                self.end_headers()

        self._http_server = _BridgeHTTPServer(("127.0.0.1", self._requested_port), Handler, bridge=self)
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        self._write_state()

    def _stop_http_server(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=1.0)
        self._remove_state()

    async def run_stdio_async(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._run_with_bridge(read_stream, write_stream)

    async def _run_with_bridge(self, read_stream, write_stream) -> None:
        async with AsyncExitStack() as stack:
            lifespan_context = await stack.enter_async_context(self.lifespan(self))
            session = await stack.enter_async_context(
                ServerSession(
                    read_stream,
                    write_stream,
                    self.create_initialization_options(experimental_capabilities=_CHANNEL_EXPERIMENTAL_CAPABILITIES),
                )
            )
            self._session = session
            self._loop = asyncio.get_running_loop()
            self._start_http_server()
            stack.callback(self._stop_http_server)
            async with anyio.create_task_group() as tg:
                async for message in session.incoming_messages:
                    tg.start_soon(
                        self._handle_message,
                        message,
                        session,
                        lifespan_context,
                        False,
                    )


def _run_bridge_server(
    *,
    session_id: str | None,
    provider_session_id: str | None,
    state_root: Path,
    port: int,
    auth_token: str,
    claude_pid: int | None,
) -> None:
    server = _ChannelBridgeServer(
        session_id=session_id,
        provider_session_id=provider_session_id,
        state_root=state_root,
        port=port,
        auth_token=auth_token,
        claude_pid=claude_pid,
    )
    anyio.run(server.run_stdio_async)


@app.command("serve")
def serve(
    session_id: str | None = typer.Option(None, "--session-id", envvar="LONGHOUSE_CHANNEL_SESSION_ID"),
    provider_session_id: str | None = typer.Option(
        None,
        "--provider-session-id",
        envvar="LONGHOUSE_PROVIDER_SESSION_ID",
    ),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    port: int = typer.Option(0, "--port", min=0, max=65535),
    auth_token: str | None = typer.Option(None, "--auth-token", envvar="LONGHOUSE_CHANNEL_AUTH_TOKEN"),
    claude_pid: int | None = typer.Option(None, "--claude-pid", envvar="LONGHOUSE_CHANNEL_PARENT_PID"),
) -> None:
    """Run the local MCP channel bridge that Claude connects to over stdio."""

    resolved_state_root = resolve_claude_channel_state_root(state_root=state_root)
    _run_bridge_server(
        session_id=session_id,
        provider_session_id=provider_session_id,
        state_root=resolved_state_root,
        port=port,
        auth_token=str(auth_token or secrets.token_urlsafe(24)),
        claude_pid=claude_pid or os.getppid(),
    )


@app.command("launch", hidden=True)
def launch(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    provider_session_id: str | None = typer.Option(None, "--provider-session-id", help="Claude provider session ID."),
    cwd: Path = typer.Option(..., "--cwd", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    api_url: str = typer.Option(..., "--api-url", help="Longhouse API URL."),
    api_token: str = typer.Option(
        ...,
        "--api-token",
        envvar="LONGHOUSE_CLAUDE_REMOTE_LAUNCH_TOKEN",
        help="Longhouse device token.",
    ),
    claude_dir: Path | None = typer.Option(None, "--claude-dir", file_okay=False, dir_okay=True, resolve_path=True),
    wait_ready_secs: float = typer.Option(20.0, "--wait-ready-secs", min=0.1),
) -> None:
    """Launch a detached Claude channel session for the Machine Agent control path."""

    from zerg.cli.claude import _launch_detached_native_claude_channel

    normalized_provider_session_id = str(provider_session_id or session_id).strip()
    try:
        result = _launch_detached_native_claude_channel(
            session_id=session_id,
            provider_session_id=normalized_provider_session_id,
            cwd=cwd,
            base_url=api_url,
            token=api_token,
            config_dir=claude_dir,
            wait_ready_secs=wait_ready_secs,
        )
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "provider_launch_failed",
                        "message": str(exc),
                    },
                }
            ),
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(result, default=str))


@app.command("send")
def send(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    text: str = typer.Option(..., "--text", help="Message text to inject into Claude."),
    meta: list[str] | None = typer.Option(None, "--meta", help="Repeatable key=value metadata entries."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_SEND_WAIT_SECS, "--wait-secs", min=0.0),
) -> None:
    """Send a live message into the active Claude channel bridge."""

    state = wait_for_claude_channel_state(
        session_id=session_id,
        timeout_secs=wait_secs,
        state_root=state_root,
    )
    port = int(state.get("port") or 0)
    auth_token = str(state.get("auth_token") or "")
    if port <= 0:
        typer.secho("Claude channel state is missing a valid port.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    payload = {
        "content": text,
        "meta": {
            "injected_by": "longhouse",
            "longhouse_session_id": session_id,
            **_parse_meta(meta),
        },
    }
    with httpx.Client(timeout=_DEFAULT_HTTP_TIMEOUT_SECS) as client:
        response = client.post(
            f"http://127.0.0.1:{port}/inject",
            headers={"X-Longhouse-Channel-Token": auth_token},
            json=payload,
        )
    if response.status_code != 204:
        detail = response.text.strip() or f"bridge returned {response.status_code}"
        typer.secho(detail, fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command("interrupt")
def interrupt(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
    wait_secs: float = typer.Option(_DEFAULT_SEND_WAIT_SECS, "--wait-secs", min=0.0),
) -> None:
    """Send SIGINT to the Claude process associated with the bridge state."""

    state = wait_for_claude_channel_state(
        session_id=session_id,
        timeout_secs=wait_secs,
        state_root=state_root,
    )
    claude_pid = int(state.get("claude_pid") or 0)
    if claude_pid <= 0:
        typer.secho("Claude channel state is missing claude_pid.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    try:
        os.kill(claude_pid, signal.SIGINT)
    except ProcessLookupError:
        typer.secho(f"Claude process {claude_pid} is not running.", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command("inspect")
def inspect_state(
    session_id: str = typer.Option(..., "--session-id", help="Longhouse session ID."),
    state_root: Path | None = typer.Option(None, "--state-root", file_okay=False, dir_okay=True, resolve_path=True),
) -> None:
    """Print the current local bridge state JSON."""

    payload = read_claude_channel_state(session_id=session_id, state_root=state_root)
    typer.echo(json.dumps(payload, indent=2))
