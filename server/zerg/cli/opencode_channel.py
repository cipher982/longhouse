"""Hidden OpenCode server-bridge control commands."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import time
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen
from uuid import UUID

import typer

from zerg.cli.opencode import _managed_runtime_events_url
from zerg.cli.opencode import _OpenCodeLaunchError
from zerg.cli.opencode import _resolve_opencode_binary
from zerg.cli.opencode import _write_opencode_runtime_config_content

app = typer.Typer(no_args_is_help=True)

OPENCODE_REMOTE_LAUNCH_TOKEN_ENV = "LONGHOUSE_OPENCODE_REMOTE_LAUNCH_TOKEN"
OPENCODE_SERVER_BRIDGE_TRANSPORT = "opencode_server_bridge"
_DEFAULT_USERNAME = "opencode"
# Stays 1: the identity / launch_mode / owner fields added for terminal-owned
# lifecycle are all optional with safe defaults on read, so v1 readers (older
# local components during a mixed dogfood) tolerate them. Bumping would make
# those older readers reject otherwise-compatible state and lose attach/stop.
_STATE_SCHEMA_VERSION = 1

# Lifecycle ownership of the backing `opencode serve` process.
LAUNCH_MODE_ATTACHED_TUI = "attached_tui"  # server dies when the attach TUI exits
LAUNCH_MODE_KEEP_SERVER = "keep_server"  # persistent reattachable background server
LAUNCH_MODE_DETACHED = "detached"  # no TUI in this process; server left for reattach
_VALID_LAUNCH_MODES = frozenset({LAUNCH_MODE_ATTACHED_TUI, LAUNCH_MODE_KEEP_SERVER, LAUNCH_MODE_DETACHED})
_SERVER_LOG_RE = re.compile(r"opencode server listening on (?P<url>http://127\.0\.0\.1:\d+)")
_HTTP_TIMEOUT_SECONDS = 10


class OpenCodeServerBridgeError(RuntimeError):
    """Expected OpenCode server-bridge failure."""


@dataclass(frozen=True)
class OpenCodeServerBridgeState:
    schema_version: int
    session_id: str
    provider_session_id: str
    server_url: str
    pid: int
    cwd: str
    username: str
    password: str
    log_path: str
    config_content_path: str
    started_at: str
    updated_at: str
    # Process identity for PID-reuse-safe kills (schema v2+). Empty on legacy
    # state files, which then fall back to a bare liveness check.
    process_start_time: str = ""
    process_command: str = ""
    # Lifecycle ownership (schema v2+). launch_mode is one of
    # attached_tui | keep_server | detached. owner_wrapper_* identify the
    # `longhouse opencode` wrapper process whose exit should stop an
    # attached_tui server; used by the engine reaper as a crash backstop.
    launch_mode: str = ""
    owner_wrapper_pid: int = 0
    owner_wrapper_start_time: str = ""

    @classmethod
    def from_mapping(cls, payload: dict) -> "OpenCodeServerBridgeState":
        return cls(
            schema_version=int(payload.get("schema_version") or 0),
            session_id=str(payload.get("session_id") or ""),
            provider_session_id=str(payload.get("provider_session_id") or ""),
            server_url=str(payload.get("server_url") or ""),
            pid=int(payload.get("pid") or 0),
            cwd=str(payload.get("cwd") or ""),
            username=str(payload.get("username") or _DEFAULT_USERNAME),
            password=str(payload.get("password") or ""),
            log_path=str(payload.get("log_path") or ""),
            config_content_path=str(payload.get("config_content_path") or ""),
            started_at=str(payload.get("started_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            process_start_time=str(payload.get("process_start_time") or ""),
            process_command=str(payload.get("process_command") or ""),
            launch_mode=str(payload.get("launch_mode") or ""),
            owner_wrapper_pid=int(payload.get("owner_wrapper_pid") or 0),
            owner_wrapper_start_time=str(payload.get("owner_wrapper_start_time") or ""),
        )

    def redacted(self) -> dict:
        payload = asdict(self)
        payload["password"] = "***"
        return payload


def _utc_now() -> str:
    from datetime import datetime
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_session_id(session_id: str) -> str:
    normalized = str(session_id or "").strip()
    try:
        UUID(normalized)
    except ValueError as exc:
        raise OpenCodeServerBridgeError("session_id must be a UUID") from exc
    return normalized


def _opencode_server_state_dir(config_dir: Path | None = None) -> Path:
    return (config_dir or (Path.home() / ".claude")) / "managed-local" / "opencode-server"


def _opencode_server_state_path(session_id: str, config_dir: Path | None = None) -> Path:
    return _opencode_server_state_dir(config_dir) / f"{session_id}.json"


def _opencode_server_lock_path(session_id: str, config_dir: Path | None = None) -> Path:
    return _opencode_server_state_dir(config_dir) / f"{session_id}.lock"


@contextlib.contextmanager
def _opencode_server_launch_lock(session_id: str, config_dir: Path | None = None):
    path = _opencode_server_lock_path(session_id, config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
    finally:
        pass


def _write_private_json(path: Path, payload: dict) -> None:
    """Atomically write private JSON state.

    The engine scanner skips state files that fail to parse, so a truncating
    in-place write can race the scanner and cause a transient false detach.
    Write to a temp file, fsync, then atomically replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def read_opencode_server_bridge_state(
    session_id: str,
    *,
    config_dir: Path | None = None,
) -> OpenCodeServerBridgeState:
    normalized = _validate_session_id(session_id)
    path = _opencode_server_state_path(normalized, config_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OpenCodeServerBridgeError(f"OpenCode server bridge state not found for {normalized}") from exc
    except json.JSONDecodeError as exc:
        raise OpenCodeServerBridgeError(f"OpenCode server bridge state is not valid JSON: {path}") from exc
    state = OpenCodeServerBridgeState.from_mapping(payload)
    if state.schema_version > _STATE_SCHEMA_VERSION:
        message = f"OpenCode server bridge state schema {state.schema_version} is newer than this Longhouse build"
        raise OpenCodeServerBridgeError(message)
    if state.session_id != normalized:
        raise OpenCodeServerBridgeError("OpenCode server bridge state session_id mismatch")
    if not state.provider_session_id or not state.server_url or not state.password:
        raise OpenCodeServerBridgeError("OpenCode server bridge state is incomplete")
    return state


def _authorization_header(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _api_url(server_url: str, path: str, *, query: dict[str, str] | None = None) -> str:
    base = server_url.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    if query:
        return f"{base}{suffix}?{urlencode(query)}"
    return f"{base}{suffix}"


def _request_opencode_json(
    *,
    server_url: str,
    username: str,
    password: str,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: int = _HTTP_TIMEOUT_SECONDS,
):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": _authorization_header(username, password),
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        _api_url(server_url, path, query=query),
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise OpenCodeServerBridgeError(f"OpenCode server request failed: {exc}") from exc
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenCodeServerBridgeError("OpenCode server returned invalid JSON") from exc


def _tail_text(path: Path, *, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _wait_for_server_url(log_path: Path, process: subprocess.Popen, *, timeout_secs: int) -> str:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        match = _SERVER_LOG_RE.search(_tail_text(log_path))
        if match:
            return match.group("url")
        if process.poll() is not None:
            detail = _tail_text(log_path).strip()
            raise OpenCodeServerBridgeError(f"OpenCode server exited before it became ready: {detail}")
        time.sleep(0.1)
    detail = _tail_text(log_path).strip()
    raise OpenCodeServerBridgeError(f"Timed out waiting for OpenCode server URL after {timeout_secs}s: {detail}")


def _assert_health_ready(*, server_url: str, username: str, password: str) -> None:
    payload = _request_opencode_json(
        server_url=server_url,
        username=username,
        password=password,
        method="GET",
        path="/global/health",
    )
    if not isinstance(payload, dict) or payload.get("healthy") is not True:
        raise OpenCodeServerBridgeError("OpenCode server health check did not report healthy")


def _create_opencode_session(
    *,
    server_url: str,
    username: str,
    password: str,
    cwd: Path,
    title: str,
) -> str:
    payload = _request_opencode_json(
        server_url=server_url,
        username=username,
        password=password,
        method="POST",
        path="/session",
        query={"directory": str(cwd)},
        payload={"title": title},
    )
    if not isinstance(payload, dict) or not str(payload.get("id") or "").strip():
        raise OpenCodeServerBridgeError("OpenCode session.create returned no session id")
    return str(payload["id"])


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_identity(pid: int) -> tuple[str, str] | None:
    """Return (lstart, command) for a live pid via ``ps``, or None if gone.

    Used to defend kill paths against PID reuse: a recorded pid is only the
    process we launched if its start time and command still match what we
    recorded. ``lstart`` is the fixed-width 24-char start-time field, mirroring
    the engine's ``managed_claude_scan`` PID-reuse defense.
    """
    if pid <= 0:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=,command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = completed.stdout.strip()
    if completed.returncode != 0 or len(line) <= 24:
        return None
    lstart = line[:24].strip()
    command = line[24:].strip()
    if not command:
        return None
    return (lstart, command)


def _pid_matches_recorded_identity(state: "OpenCodeServerBridgeState") -> bool:
    """True iff state.pid is live AND is provably the server we launched.

    Never returns True on bare liveness alone — a reused PID must not be killed.
    With recorded identity (schema v2), require an exact start-time + command
    match. Without it (legacy v1 state, or a v2 launch where `ps` failed at
    capture time), fall back to confirming the live PID is still an
    `opencode serve` process, which is a weaker but real reuse defense.
    """
    if not _pid_is_running(state.pid):
        return False
    identity = _process_identity(state.pid)
    if identity is None:
        # ps could not confirm identity; do not kill a possibly-reused pid.
        return False
    live_start, live_cmd = identity
    recorded_start = (state.process_start_time or "").strip()
    recorded_cmd = (state.process_command or "").strip()
    if not recorded_start and not recorded_cmd:
        # No recorded identity: only stop if the live process still looks like
        # an OpenCode server, so a recycled PID for an unrelated process is safe.
        return "opencode" in live_cmd and " serve" in live_cmd
    if recorded_start and recorded_start != live_start:
        return False
    if recorded_cmd and recorded_cmd != live_cmd:
        return False
    return True


def _state_result(state: OpenCodeServerBridgeState) -> dict:
    return {
        "session_id": state.session_id,
        "provider": "opencode",
        "transport": OPENCODE_SERVER_BRIDGE_TRANSPORT,
        "provider_session_id": state.provider_session_id,
        "server_url": state.server_url,
        "pid": state.pid,
        "log_path": state.log_path,
    }


def _existing_live_state_result(
    *,
    session_id: str,
    config_dir: Path | None,
) -> dict | None:
    try:
        state = read_opencode_server_bridge_state(session_id, config_dir=config_dir)
    except OpenCodeServerBridgeError:
        return None
    if not _pid_matches_recorded_identity(state):
        return None
    try:
        _assert_health_ready(server_url=state.server_url, username=state.username, password=state.password)
    except OpenCodeServerBridgeError:
        # Only kill if the pid is provably still the server we launched.
        if _pid_matches_recorded_identity(state):
            _terminate_pid(state.pid)
        return None
    return _state_result(state)


def launch_opencode_server_bridge(
    *,
    session_id: str,
    cwd: Path,
    api_url: str,
    api_token: str,
    device_id: str,
    display_name: str | None = None,
    opencode_bin: str | None = None,
    config_dir: Path | None = None,
    wait_ready_secs: int = 45,
    # Default to detached: a bare bridge launch (remote/headless control path)
    # has no TUI in this process and is a background reattachable server. The
    # interactive `longhouse opencode` wrapper passes attached_tui explicitly
    # with its owner pid. Defaulting to attached_tui here would mislabel
    # remote-launched servers as foreground TUIs.
    launch_mode: str = LAUNCH_MODE_DETACHED,
    owner_wrapper_pid: int | None = None,
) -> dict:
    normalized_session_id = _validate_session_id(session_id)
    if launch_mode not in _VALID_LAUNCH_MODES:
        raise OpenCodeServerBridgeError(f"unknown launch_mode: {launch_mode!r}")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise OpenCodeServerBridgeError("cwd must be an existing absolute directory")
    if not str(api_token or "").strip():
        raise OpenCodeServerBridgeError("api token is required")
    resolved_bin = _resolve_opencode_binary(opencode_bin)
    if not resolved_bin:
        raise OpenCodeServerBridgeError("OpenCode executable not found")

    state_dir = _opencode_server_state_dir(config_dir)
    logs_dir = state_dir / "logs"
    with _opencode_server_launch_lock(normalized_session_id, config_dir):
        existing = _existing_live_state_result(session_id=normalized_session_id, config_dir=config_dir)
        if existing is not None:
            return existing

        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"{normalized_session_id}.log"
        config_content_path = _write_opencode_runtime_config_content(
            config_dir=config_dir,
            runtime_events_url=_managed_runtime_events_url(api_url),
            token=api_token,
            session_id=normalized_session_id,
            device_id=device_id,
        )

        username = _DEFAULT_USERNAME
        password = secrets.token_urlsafe(24)
        env = os.environ.copy()
        env["LONGHOUSE_MANAGED_SESSION_ID"] = normalized_session_id
        env["LONGHOUSE_DEVICE_ID"] = device_id
        env["OPENCODE_CONFIG_CONTENT"] = config_content_path.read_text(encoding="utf-8")
        env["OPENCODE_SERVER_USERNAME"] = username
        env["OPENCODE_SERVER_PASSWORD"] = password

        cmd = [
            resolved_bin,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            "--print-logs",
        ]
        process: subprocess.Popen | None = None
        try:
            with log_path.open("ab", buffering=0) as log_file:
                process = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            server_url = _wait_for_server_url(log_path, process, timeout_secs=wait_ready_secs)
            _assert_health_ready(server_url=server_url, username=username, password=password)
            title = (display_name or cwd.name or normalized_session_id).strip()
            provider_session_id = _create_opencode_session(
                server_url=server_url,
                username=username,
                password=password,
                cwd=cwd,
                title=title,
            )
            now = _utc_now()
            identity = _process_identity(int(process.pid))
            resolved_owner_pid = int(owner_wrapper_pid or 0)
            owner_identity = _process_identity(resolved_owner_pid) if resolved_owner_pid > 0 else None
            state = OpenCodeServerBridgeState(
                schema_version=_STATE_SCHEMA_VERSION,
                session_id=normalized_session_id,
                provider_session_id=provider_session_id,
                server_url=server_url,
                pid=int(process.pid),
                cwd=str(cwd),
                username=username,
                password=password,
                log_path=str(log_path),
                config_content_path=str(config_content_path),
                started_at=now,
                updated_at=now,
                process_start_time=identity[0] if identity else "",
                process_command=identity[1] if identity else "",
                launch_mode=launch_mode,
                owner_wrapper_pid=resolved_owner_pid,
                owner_wrapper_start_time=owner_identity[0] if owner_identity else "",
            )
            _write_private_json(_opencode_server_state_path(normalized_session_id, config_dir), asdict(state))
        except Exception:
            if process is not None and process.poll() is None:
                try:
                    _terminate_pid(process.pid)
                except OpenCodeServerBridgeError:
                    pass
            raise

        return _state_result(state)


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as group_exc:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise OpenCodeServerBridgeError(f"Could not terminate OpenCode server pid={pid}: {exc}") from group_exc


def send_opencode_text(
    *,
    session_id: str,
    text: str,
    config_dir: Path | None = None,
) -> dict:
    state = read_opencode_server_bridge_state(session_id, config_dir=config_dir)
    _request_opencode_json(
        server_url=state.server_url,
        username=state.username,
        password=state.password,
        method="POST",
        path=f"/session/{quote(state.provider_session_id, safe='')}/prompt_async",
        query={"directory": state.cwd},
        payload={"noReply": True, "parts": [{"type": "text", "text": text}]},
    )
    return {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "provider": "opencode",
        "transport": OPENCODE_SERVER_BRIDGE_TRANSPORT,
        "provider_session_id": state.provider_session_id,
    }


def interrupt_opencode_session(
    *,
    session_id: str,
    config_dir: Path | None = None,
) -> dict:
    state = read_opencode_server_bridge_state(session_id, config_dir=config_dir)
    _request_opencode_json(
        server_url=state.server_url,
        username=state.username,
        password=state.password,
        method="POST",
        path=f"/session/{quote(state.provider_session_id, safe='')}/abort",
        query={"directory": state.cwd},
    )
    return {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "provider": "opencode",
        "transport": OPENCODE_SERVER_BRIDGE_TRANSPORT,
        "provider_session_id": state.provider_session_id,
    }


def stop_opencode_server_bridge(
    *,
    session_id: str,
    config_dir: Path | None = None,
) -> dict:
    state = read_opencode_server_bridge_state(session_id, config_dir=config_dir)
    # Defend against PID reuse: only signal the pid if it is provably still the
    # OpenCode server we launched. A no-longer-matching pid is treated as an
    # already-stopped bridge.
    matched = _pid_matches_recorded_identity(state)
    if matched:
        _terminate_pid(state.pid)
    return {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "provider": "opencode",
        "transport": OPENCODE_SERVER_BRIDGE_TRANSPORT,
        "pid": state.pid,
        "stopped": matched,
    }


class OpenCodeServerBridgeStopper:
    """Stops the backing OpenCode server when the attach TUI exits.

    Mirrors the Codex ``_CodexBridgeStopper`` shape (idempotent stop, signal
    cleanup) but deliberately has NO active-turn-survival check: OpenCode keeps
    no durable local turn state, and the terminal-owned contract is "TUI exited
    => server stops". An in-flight remote send is best-effort protected only by
    the engine reaper's optional busy preflight, not here.
    """

    def __init__(self, session_id: str, *, config_dir: Path | None = None) -> None:
        self.session_id = session_id
        self.config_dir = config_dir
        self._stopped = False

    def stop_for_terminal_disconnect(self) -> str | None:
        if self._stopped:
            return None
        self._stopped = True
        try:
            stop_opencode_server_bridge(session_id=self.session_id, config_dir=self.config_dir)
        except OpenCodeServerBridgeError as exc:
            return str(exc)
        return None


def _install_opencode_signal_cleanup(stopper: OpenCodeServerBridgeStopper) -> dict:
    """Stop the server on SIGHUP/SIGTERM so closing the terminal tears it down."""
    previous_handlers: dict = {}

    def cleanup_and_exit(signum: int, _frame: object) -> None:
        stopper.stop_for_terminal_disconnect()
        raise SystemExit(128 + signum)

    for signame in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        previous_handlers[sig] = signal.signal(sig, cleanup_and_exit)
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict) -> None:
    for sig, handler in previous_handlers.items():
        signal.signal(sig, handler)


def run_opencode_attach(
    *,
    session_id: str,
    opencode_bin: str | None = None,
    config_dir: Path | None = None,
    extra_args: tuple[str, ...] = (),
) -> int:
    state = read_opencode_server_bridge_state(session_id, config_dir=config_dir)
    _assert_health_ready(server_url=state.server_url, username=state.username, password=state.password)
    resolved_bin = _resolve_opencode_binary(opencode_bin)
    if not resolved_bin:
        raise OpenCodeServerBridgeError("OpenCode executable not found")
    env = os.environ.copy()
    env["OPENCODE_SERVER_USERNAME"] = state.username
    env["OPENCODE_SERVER_PASSWORD"] = state.password
    cmd = [
        resolved_bin,
        "attach",
        state.server_url,
        "--session",
        state.provider_session_id,
        *extra_args,
    ]
    completed = subprocess.run(cmd, cwd=state.cwd, env=env, check=False)
    return int(completed.returncode)


@app.command(name="launch")
def launch_command(
    session_id: str = typer.Option(..., "--session-id"),
    cwd: Path = typer.Option(..., "--cwd", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    api_url: str = typer.Option(..., "--api-url"),
    api_token: str | None = typer.Option(None, "--api-token", hidden=True),
    device_id: str = typer.Option(..., "--device-id"),
    display_name: str | None = typer.Option(None, "--display-name"),
    config_dir: str | None = typer.Option(None, "--config-dir", "--claude-dir"),
    opencode_bin: str | None = typer.Option(None, "--opencode-bin"),
    wait_ready_secs: int = typer.Option(45, "--wait-ready-secs"),
) -> None:
    token = (api_token or os.environ.get(OPENCODE_REMOTE_LAUNCH_TOKEN_ENV) or "").strip()
    try:
        payload = launch_opencode_server_bridge(
            session_id=session_id,
            cwd=cwd,
            api_url=api_url,
            api_token=token,
            device_id=device_id,
            display_name=display_name,
            opencode_bin=opencode_bin,
            config_dir=Path(config_dir) if config_dir else None,
            wait_ready_secs=wait_ready_secs,
        )
    except (_OpenCodeLaunchError, OpenCodeServerBridgeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(payload, sort_keys=True))


@app.command(name="send")
def send_command(
    session_id: str = typer.Option(..., "--session-id"),
    text: str = typer.Option(..., "--text"),
    config_dir: str | None = typer.Option(None, "--config-dir", "--claude-dir"),
) -> None:
    try:
        payload = send_opencode_text(
            session_id=session_id,
            text=text,
            config_dir=Path(config_dir) if config_dir else None,
        )
    except OpenCodeServerBridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(payload, sort_keys=True))


@app.command(name="interrupt")
def interrupt_command(
    session_id: str = typer.Option(..., "--session-id"),
    config_dir: str | None = typer.Option(None, "--config-dir", "--claude-dir"),
) -> None:
    try:
        payload = interrupt_opencode_session(
            session_id=session_id,
            config_dir=Path(config_dir) if config_dir else None,
        )
    except OpenCodeServerBridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(payload, sort_keys=True))


@app.command(name="attach", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def attach_command(
    ctx: typer.Context,
    session_id: str = typer.Option(..., "--session-id"),
    config_dir: str | None = typer.Option(None, "--config-dir", "--claude-dir"),
    opencode_bin: str | None = typer.Option(None, "--opencode-bin"),
) -> None:
    try:
        code = run_opencode_attach(
            session_id=session_id,
            opencode_bin=opencode_bin,
            config_dir=Path(config_dir) if config_dir else None,
            extra_args=tuple(str(arg) for arg in (ctx.args or ())),
        )
    except (_OpenCodeLaunchError, OpenCodeServerBridgeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if code != 0:
        raise typer.Exit(code=code)


@app.command(name="inspect")
def inspect_command(
    session_id: str = typer.Option(..., "--session-id"),
    config_dir: str | None = typer.Option(None, "--config-dir", "--claude-dir"),
) -> None:
    try:
        state = read_opencode_server_bridge_state(
            session_id=session_id,
            config_dir=Path(config_dir) if config_dir else None,
        )
    except OpenCodeServerBridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(state.redacted(), indent=2, sort_keys=True))


@app.command(name="stop")
def stop_command(
    session_id: str = typer.Option(..., "--session-id"),
    config_dir: str | None = typer.Option(None, "--config-dir", "--claude-dir"),
) -> None:
    try:
        payload = stop_opencode_server_bridge(
            session_id=session_id,
            config_dir=Path(config_dir) if config_dir else None,
        )
    except OpenCodeServerBridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(payload, sort_keys=True))


__all__ = [
    "LAUNCH_MODE_ATTACHED_TUI",
    "LAUNCH_MODE_DETACHED",
    "LAUNCH_MODE_KEEP_SERVER",
    "OPENCODE_REMOTE_LAUNCH_TOKEN_ENV",
    "OPENCODE_SERVER_BRIDGE_TRANSPORT",
    "OpenCodeServerBridgeError",
    "OpenCodeServerBridgeState",
    "OpenCodeServerBridgeStopper",
    "interrupt_opencode_session",
    "launch_opencode_server_bridge",
    "read_opencode_server_bridge_state",
    "run_opencode_attach",
    "send_opencode_text",
    "stop_opencode_server_bridge",
]
