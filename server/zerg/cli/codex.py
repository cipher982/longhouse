"""Longhouse Codex session launcher CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections import deque
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from urllib.request import Request
from urllib.request import urlopen

import typer

from zerg.cli import claude as managed_local_cli
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli._common import build_session_url as _build_session_url
from zerg.cli._common import ensure_managed_launch_preflight as _ensure_managed_launch_preflight
from zerg.cli._common import interactive_stdio as _interactive_stdio
from zerg.cli._common import load_api_credentials as _load_api_credentials
from zerg.cli._common import open_session_url as _open_session_url
from zerg.provider_cli_contract import CODEX_BIN_ENV
from zerg.provider_cli_contract import LEGACY_MANAGED_CODEX_LAUNCHER_MARKER
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_CODEX_BIN_FLAG
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_MISSING
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PATH
from zerg.services.session_continuity import get_machine_name_label
from zerg.services.shipper.service import get_engine_executable
from zerg.session_loop_mode import SessionLoopMode

app = typer.Typer(
    name="codex",
    help="Launch a managed Longhouse Codex session by default; subcommands inspect bridge state.",
    invoke_without_command=True,
    no_args_is_help=False,
)

_CODEX_DISABLE_UPDATE_CHECK_CONFIG = "check_for_update_on_startup=false"
_ROLLOUT_TURN_EVENT_TYPES = {"task_started", "task_complete", "turn_aborted"}
_ROLLOUT_TERMINAL_EVENT_TYPES = {"task_complete", "turn_aborted"}
_ROLLOUT_TAIL_LINES = 256
_CODEX_VERSION_TIMEOUT_SECONDS = 5
_CODEX_STOP_REASON_BRIDGE_STOP = "bridge_stop"
_CODEX_STOP_REASON_TERMINAL_DISCONNECTED = "terminal_disconnected"
_CODEX_STOP_SIGNAL_TIMEOUT_SECONDS = 3.0
_CODEX_BIN_OPTION_HELP = " ".join(
    [
        "Debug override for the Codex executable used by managed sessions",
        f"(defaults to {CODEX_BIN_ENV}, then `codex` on PATH).",
    ]
)
_CODEX_DOCTOR_BIN_OPTION_HELP = " ".join(
    [
        "Debug override for the Codex executable to inspect",
        f"(defaults to {CODEX_BIN_ENV}, then `codex` on PATH).",
    ]
)
_WARP_CLI_AGENT_OSC_TITLE = "warp://cli-agent"


def _stdio_ttys() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _warp_cli_agent_available() -> bool:
    is_warp = os.environ.get("TERM_PROGRAM") == "WarpTerminal"
    has_protocol = bool(os.environ.get("WARP_CLI_AGENT_PROTOCOL_VERSION"))
    has_client_version = bool(os.environ.get("WARP_CLIENT_VERSION"))
    return is_warp and has_protocol and has_client_version


def _emit_warp_cli_agent_event(
    *,
    event: str,
    session_id: str,
    cwd: Path,
    project: str | None,
    **extra: object,
) -> None:
    if not _warp_cli_agent_available():
        return
    payload = {
        "v": 1,
        "agent": "codex",
        "event": event,
        "session_id": session_id,
        "cwd": str(cwd),
        "project": project or cwd.name,
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    body = json.dumps(payload, separators=(",", ":"))
    marker = f"\033]777;notify;{_WARP_CLI_AGENT_OSC_TITLE};{body}\a"
    try:
        with Path("/dev/tty").open("w", encoding="utf-8") as tty:
            tty.write(marker)
            tty.flush()
    except OSError:
        if sys.stdout.isatty():
            sys.stdout.write(marker)
            sys.stdout.flush()


def _resolve_explicit_codex_binary(candidate: str, *, source: str) -> str:
    normalized = str(candidate or "").strip()
    if not normalized:
        raise _NativeBridgeError(f"{source} is empty")
    looks_like_path = normalized.startswith((".", "~", "/")) or "/" in normalized or "\\" in normalized
    if looks_like_path:
        path = Path(os.path.expanduser(normalized))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise _NativeBridgeError(f"{source} points to `{candidate}`, but it is not an executable file.")
    resolved = shutil.which(normalized)
    if resolved:
        return resolved
    raise _NativeBridgeError(f"{source} points to `{candidate}`, but it was not found on PATH.")


def _resolve_codex_binary(explicit: str | None = None) -> str | None:
    return _resolve_codex_binary_with_source(explicit)["path"]


def _resolve_codex_binary_with_source(explicit: str | None = None) -> dict[str, str | None]:
    normalized = str(explicit or "").strip()
    if normalized:
        return {
            "path": _resolve_explicit_codex_binary(normalized, source=PROVIDER_CLI_SOURCE_CODEX_BIN_FLAG),
            "source": PROVIDER_CLI_SOURCE_CODEX_BIN_FLAG,
        }
    env_candidate = str(os.environ.get(CODEX_BIN_ENV) or "").strip()
    if env_candidate:
        return {"path": _resolve_explicit_codex_binary(env_candidate, source=CODEX_BIN_ENV), "source": CODEX_BIN_ENV}
    resolved = shutil.which("codex")
    return {"path": resolved, "source": PROVIDER_CLI_SOURCE_PATH if resolved else PROVIDER_CLI_SOURCE_MISSING}


def _codex_version(codex_bin: str | None) -> dict[str, object]:
    if not codex_bin:
        return {"ok": False, "value": None, "error": "codex executable not found"}
    try:
        completed = subprocess.run(
            [codex_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_CODEX_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "value": None, "error": str(exc)}
    output = ((completed.stdout or "").strip() or (completed.stderr or "").strip()).splitlines()
    value = output[0].strip() if output else ""
    if completed.returncode == 0 and value:
        return {"ok": True, "value": value, "error": None}
    return {
        "ok": False,
        "value": value or None,
        "error": f"codex --version exited with code {completed.returncode}",
    }


def _default_codex_bridge_state_root() -> Path:
    return Path.home() / ".claude" / "managed-local" / "codex-bridge"


def _pid_alive(pid: object) -> bool | None:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _lock_file_held(lock_path: Path) -> bool | None:
    if not lock_path.exists():
        return None
    if os.name != "posix":
        return None
    try:
        import fcntl

        with lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False
    except OSError:
        return None


def _read_codex_bridge_states(state_root: Path, *, check_readyz: bool = False) -> list[dict[str, object]]:
    if not state_root.exists():
        return []
    states: list[dict[str, object]] = []
    for state_file in sorted(state_root.glob("*.json")):
        if state_file.name.endswith(".tmp"):
            continue
        try:
            state = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            states.append({"state_file": str(state_file), "readable": False})
            continue
        lock_path = state_file.with_suffix(".lock")
        ws_url = str(state.get("ws_url") or "").strip() or None
        states.append(
            {
                "session_id": str(state.get("session_id") or state_file.stem),
                "state_file": str(state_file),
                "log_file": str(state.get("log_file") or ""),
                "readable": True,
                "status": str(state.get("status") or ""),
                "pid": state.get("pid"),
                "pid_alive": _pid_alive(state.get("pid")),
                "app_server_pid": state.get("app_server_pid"),
                "app_server_pid_alive": _pid_alive(state.get("app_server_pid")),
                "app_server_pgid": state.get("app_server_pgid"),
                "app_server_ws_url": str(state.get("app_server_ws_url") or ""),
                "lock_file": str(lock_path),
                "lock_file_exists": lock_path.exists(),
                "lock_held": _lock_file_held(lock_path),
                "codex_bin": str(state.get("codex_bin") or ""),
                "ws_url": ws_url,
                "readyz_healthy": _bridge_readyz_healthy(ws_url) if check_readyz and ws_url else None,
                "thread_id": str(state.get("thread_id") or ""),
                "thread_path": str(state.get("thread_path") or ""),
                "last_turn_status": str(state.get("last_turn_status") or ""),
                "active_turn_id": str(state.get("active_turn_id") or ""),
                "updated_at": str(state.get("updated_at") or ""),
            }
        )
    return states


def _collect_codex_doctor(
    *,
    codex_bin: str | None,
    state_root: Path | None,
    check_readyz: bool = False,
) -> dict[str, object]:
    resolution = _resolve_codex_binary_with_source(codex_bin)
    resolved_codex_bin = resolution["path"]
    home = Path.home()
    legacy_launcher = home / ".local" / "bin" / "longhouse-codex"
    legacy_runtime_dir = home / ".longhouse" / "runtimes" / "codex"
    resolved_state_root = state_root or _default_codex_bridge_state_root()
    bridge_states = _read_codex_bridge_states(resolved_state_root, check_readyz=check_readyz)
    return {
        "codex_binary": {
            "path": resolved_codex_bin,
            "source": resolution["source"],
            "version": _codex_version(resolved_codex_bin),
            "env_override": os.environ.get(CODEX_BIN_ENV),
        },
        "legacy_artifacts": {
            "launcher": {
                "path": str(legacy_launcher),
                "exists": legacy_launcher.exists(),
                "legacy_marker": _legacy_codex_launcher_has_marker(legacy_launcher),
            },
            "managed_runtime_dir": {
                "path": str(legacy_runtime_dir),
                "exists": legacy_runtime_dir.exists(),
            },
        },
        "bridge": {
            "state_root": str(resolved_state_root),
            "state_root_exists": resolved_state_root.exists(),
            "readyz_checked": check_readyz,
            "sessions": bridge_states,
        },
    }


def _legacy_codex_launcher_has_marker(path: Path) -> bool:
    try:
        return path.is_file() and LEGACY_MANAGED_CODEX_LAUNCHER_MARKER in path.read_text(errors="ignore")
    except OSError:
        return False


def _render_codex_doctor(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    binary = dict(payload["codex_binary"])
    version = dict(binary.get("version") or {})
    typer.echo("Codex")
    typer.echo(f"  path: {binary.get('path') or '-'}")
    typer.echo(f"  source: {binary.get('source') or '-'}")
    typer.echo(f"  version: {version.get('value') or '-'}")
    if version.get("error"):
        typer.echo(f"  version error: {version['error']}")
    typer.echo(f"  {CODEX_BIN_ENV}: {binary.get('env_override') or '-'}")

    artifacts = dict(payload["legacy_artifacts"])
    launcher = dict(artifacts["launcher"])
    runtime = dict(artifacts["managed_runtime_dir"])
    typer.echo("")
    typer.echo("Legacy artifacts")
    typer.echo(f"  longhouse-codex: {'present' if launcher.get('exists') else 'absent'} ({launcher.get('path')})")
    if launcher.get("exists"):
        typer.echo(f"    legacy marker: {'yes' if launcher.get('legacy_marker') else 'no'}")
    typer.echo(f"  managed runtime dir: {'present' if runtime.get('exists') else 'absent'} ({runtime.get('path')})")

    bridge = dict(payload["bridge"])
    sessions = list(bridge.get("sessions") or [])
    typer.echo("")
    typer.echo("Bridge")
    typer.echo(f"  state root: {bridge.get('state_root')}")
    typer.echo(f"  readyz checked: {'yes' if bridge.get('readyz_checked') else 'no'}")
    typer.echo(f"  state files: {len(sessions)}")
    for session in sessions:
        state = dict(session)
        typer.echo(f"  - {state.get('session_id')}")
        typer.echo(f"      status: {state.get('status') or '-'}")
        typer.echo(f"      pid: {state.get('pid') or '-'} alive={state.get('pid_alive')}")
        typer.echo(
            f"      app-server: pid={state.get('app_server_pid') or '-'} "
            f"alive={state.get('app_server_pid_alive')} pgid={state.get('app_server_pgid') or '-'}"
        )
        typer.echo(f"      lock: exists={state.get('lock_file_exists')} held={state.get('lock_held')}")
        typer.echo(f"      codex: {state.get('codex_bin') or '-'}")
        typer.echo(f"      ws: {state.get('ws_url') or '-'} readyz={state.get('readyz_healthy')}")
        if state.get("app_server_ws_url"):
            typer.echo(f"      app-server ws: {state.get('app_server_ws_url')}")
        if state.get("thread_id"):
            typer.echo(f"      thread: {state['thread_id']}")


def _build_codex_attach_command(
    *,
    codex_bin: str,
    ws_url: str,
    bypass_approvals: bool,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    cmd = [codex_bin, "-c", _CODEX_DISABLE_UPDATE_CHECK_CONFIG]
    if model_reasoning_effort:
        cmd += ["-c", f"model_reasoning_effort={model_reasoning_effort}"]
    if model:
        cmd += ["--model", model]
    if bypass_approvals:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd += ["--enable", "tui_app_server", "--remote", ws_url]
    command_text = shlex.join(cmd)
    if session_id:
        return f"LONGHOUSE_MANAGED_SESSION_ID={shlex.quote(session_id)} {command_text}"
    return command_text


class _NativeBridgeError(Exception):
    """Raised when the native Codex bridge fails to start."""


def _start_native_codex_bridge(
    *,
    session_id: str,
    cwd: Path,
    url: str,
    token: str,
    codex_bin: str,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
    create_initial_thread: bool = False,
) -> tuple[str, str, str | None]:
    try:
        engine = get_engine_executable()
    except RuntimeError as exc:
        raise _NativeBridgeError(str(exc)) from exc
    cmd = [
        engine,
        "codex-bridge",
        "start",
        "--session-id",
        session_id,
        "--cwd",
        str(cwd),
        "--url",
        url,
        "--token",
        token,
        "--codex-bin",
        codex_bin,
    ]
    if model:
        cmd += ["--model", model]
    if model_reasoning_effort:
        cmd += ["--model-reasoning-effort", model_reasoning_effort]
    if create_initial_thread:
        cmd.append("--create-initial-thread")
    cmd.append("--json")
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or "Failed to start native Codex bridge"
        raise _NativeBridgeError(detail)
    try:
        payload = json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise _NativeBridgeError(f"Failed to parse native Codex bridge output: {exc}") from exc
    ws_url = str(payload.get("ws_url") or "").strip()
    if not ws_url:
        raise _NativeBridgeError("Native Codex bridge did not return ws_url")
    thread_id = str(payload.get("thread_id") or "").strip()
    if create_initial_thread and not thread_id:
        raise _NativeBridgeError("Native Codex bridge did not return thread_id")
    state_file = str(payload.get("state_file") or "").strip() or None
    return thread_id, ws_url, state_file


def _load_native_codex_bridge_state(state_file: str | None) -> dict[str, object] | None:
    if not state_file:
        return None
    try:
        return json.loads(Path(state_file).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _recent_rollout_turn_events(thread_path: str | None) -> list[tuple[str, str]]:
    rollout_path = str(thread_path or "").strip()
    if not rollout_path:
        return []
    try:
        with Path(rollout_path).open("r", encoding="utf-8", errors="replace") as handle:
            tail = deque(handle, maxlen=_ROLLOUT_TAIL_LINES)
    except OSError:
        return []

    events: list[tuple[str, str]] = []
    for raw_line in reversed(tail):
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "event_msg":
            continue
        message = payload.get("payload")
        if not isinstance(message, dict):
            continue
        event_type = str(message.get("type") or "").strip()
        turn_id = str(message.get("turn_id") or "").strip()
        if event_type in _ROLLOUT_TURN_EVENT_TYPES and turn_id:
            events.append((event_type, turn_id))
    return events


def _rollout_turn_reached_terminal(thread_path: str | None, *, turn_id: str) -> bool:
    for event_type, event_turn_id in _recent_rollout_turn_events(thread_path):
        if event_turn_id != turn_id:
            continue
        if event_type in _ROLLOUT_TERMINAL_EVENT_TYPES:
            return True
        if event_type == "task_started":
            return False
    return False


def _latest_rollout_turn_is_terminal(thread_path: str | None) -> bool:
    for event_type, _turn_id in _recent_rollout_turn_events(thread_path):
        return event_type in _ROLLOUT_TERMINAL_EVENT_TYPES
    return False


def _bridge_readyz_url(ws_url: str | None) -> str | None:
    normalized = str(ws_url or "").strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.scheme not in {"ws", "wss", "http", "https"}:
        return None
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    path = parsed.path.rstrip("/")
    readyz_path = f"{path}/readyz" if path else "/readyz"
    return urlunsplit((scheme, parsed.netloc, readyz_path, "", ""))


def _bridge_readyz_healthy(ws_url: str | None, *, timeout_secs: float = 1.0) -> bool:
    readyz_url = _bridge_readyz_url(ws_url)
    if not readyz_url:
        return False
    request = Request(readyz_url, method="GET")
    try:
        with urlopen(request, timeout=timeout_secs) as response:
            status = getattr(response, "status", None) or response.getcode()
            return 200 <= int(status) < 300
    except (HTTPError, URLError, OSError, ValueError):
        return False


def _active_turn_survived_tui_exit(state_file: str | None) -> bool:
    state = _load_native_codex_bridge_state(state_file)
    if not state:
        return False
    if str(state.get("status") or "").strip() != "ready":
        return False
    if not _bridge_readyz_healthy(state.get("ws_url")):
        return False
    if not str(state.get("thread_id") or "").strip():
        return False
    thread_path = str(state.get("thread_path") or "").strip() or None
    active_turn_id = str(state.get("active_turn_id") or "").strip()
    if active_turn_id:
        if _rollout_turn_reached_terminal(thread_path, turn_id=active_turn_id):
            return False
        return True
    if str(state.get("last_turn_status") or "").strip() != "inProgress":
        return False
    return not _latest_rollout_turn_is_terminal(thread_path)


def _stop_native_codex_bridge(
    *,
    session_id: str,
    reason: str,
    timeout_secs: float | None = None,
) -> str | None:
    try:
        engine = get_engine_executable()
    except RuntimeError as exc:
        return str(exc)
    command = [
        engine,
        "codex-bridge",
        "stop",
        "--session-id",
        session_id,
        "--reason",
        reason,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
    except subprocess.TimeoutExpired:
        return f"codex-bridge stop timed out after {timeout_secs}s"
    if completed.returncode == 0:
        return None
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    return stderr or stdout or f"codex-bridge stop exited with code {completed.returncode}"


class _CodexBridgeStopper:
    def __init__(self, session_id: str, *, state_file: str | None = None) -> None:
        self.session_id = session_id
        self.state_file = state_file
        self._stopped = False

    def stop(self, *, reason: str, timeout_secs: float | None = None) -> str | None:
        if self._stopped:
            return None
        self._stopped = True
        return _stop_native_codex_bridge(
            session_id=self.session_id,
            reason=reason,
            timeout_secs=timeout_secs,
        )

    def stop_for_terminal_disconnect(self, *, timeout_secs: float | None = None) -> str | None:
        if self._stopped:
            return None
        if _active_turn_survived_tui_exit(self.state_file):
            self._stopped = True
            return None
        return self.stop(
            reason=_CODEX_STOP_REASON_TERMINAL_DISCONNECTED,
            timeout_secs=timeout_secs,
        )


def _install_codex_signal_cleanup(stopper: _CodexBridgeStopper) -> dict[signal.Signals, object]:
    previous_handlers: dict[signal.Signals, object] = {}

    def cleanup_and_exit(signum: int, _frame: object) -> None:
        stopper.stop_for_terminal_disconnect(
            timeout_secs=_CODEX_STOP_SIGNAL_TIMEOUT_SECONDS,
        )
        raise SystemExit(128 + signum)

    for signame in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        previous_handlers[sig] = signal.signal(sig, cleanup_and_exit)
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[signal.Signals, object]) -> None:
    for sig, handler in previous_handlers.items():
        signal.signal(sig, handler)


def _run_native_codex_tui(
    *,
    session_id: str,
    codex_bin: str,
    ws_url: str,
    cwd: Path,
    bypass_approvals: bool = False,
    model: str | None = None,
    model_reasoning_effort: str | None = None,
    thread_id: str | None = None,
) -> int:
    # Connect TUI to the bridge's app-server. The bridge has already created the
    # active thread; passing `resume <thread_id>` would make Codex resolve a
    # local rollout file that may not exist for bridge-created sessions.
    cmd = [codex_bin, "-c", _CODEX_DISABLE_UPDATE_CHECK_CONFIG]
    if model_reasoning_effort:
        cmd += ["-c", f"model_reasoning_effort={model_reasoning_effort}"]
    if model:
        cmd += ["--model", model]
    if bypass_approvals:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd += ["--enable", "tui_app_server", "--remote", ws_url]
    env = os.environ.copy()
    env["LONGHOUSE_MANAGED_SESSION_ID"] = session_id
    if os.name == "posix" and _stdio_ttys():
        return _run_foreground_process_group(cmd=cmd, cwd=cwd, env=env)
    completed = subprocess.run(cmd, check=False, cwd=str(cwd), env=env)
    return int(completed.returncode)


def _run_foreground_process_group(*, cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
    """Run an interactive child as the terminal foreground job.

    Warp keys Codex session affordances off the foreground process group. If
    `longhouse codex` keeps the Python wrapper as that group leader, Warp sees
    Longhouse instead of Codex even though a Codex TUI child is running.
    """

    stdin_fd = sys.stdin.fileno()
    parent_pgrp = os.getpgrp()

    def make_child_group() -> None:
        os.setpgrp()

    child = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        preexec_fn=make_child_group,
    )
    child_pgrp = child.pid
    try:
        os.setpgid(child.pid, child_pgrp)
    except OSError:
        # The child may already have completed setpgrp+exec by the time the
        # parent resumes. In that normal race, the desired process group exists.
        pass

    old_sigttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    foreground_handed_off = False
    try:
        os.tcsetpgrp(stdin_fd, child_pgrp)
        foreground_handed_off = True
        return int(child.wait())
    finally:
        if foreground_handed_off:
            try:
                os.tcsetpgrp(stdin_fd, parent_pgrp)
            except OSError:
                pass
        signal.signal(signal.SIGTTOU, old_sigttou)


def _launch_managed_local_from_api(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    loop_mode: SessionLoopMode,
    name: str | None,
    machine_name: str,
) -> ManagedLocalLaunchResponse:
    return managed_local_cli._launch_managed_local_from_api(
        url=url,
        token=token,
        cwd=cwd,
        project=project,
        loop_mode=loop_mode,
        name=name,
        machine_name=machine_name,
        provider="codex",
    )


@app.callback()
def codex(
    ctx: typer.Context,
    cwd: Path = typer.Option(
        Path("."),
        "--cwd",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Working directory to launch from (defaults to current directory).",
    ),
    project: str | None = typer.Option(None, "--project", help="Optional session project label."),
    loop_mode: SessionLoopMode = typer.Option(
        SessionLoopMode.ASSIST,
        "--loop-mode",
        help="Loop mode to store on the Longhouse session.",
    ),
    name: str | None = typer.Option(None, "--name", help="Optional display name for the Codex session."),
    attach: bool = typer.Option(
        True,
        "--attach/--no-attach",
        help="Auto-attach to the Longhouse session when running interactively.",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open/--no-open",
        help="Open the session detail page in the default browser after launch.",
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="Device token (uses stored token if not specified)",
    ),
    config_dir: str | None = typer.Option(
        None,
        "--config-dir",
        "--codex-dir",
        "--claude-dir",
        help="Longhouse config directory (default: ~/.claude).",
    ),
    codex_bin: str | None = typer.Option(
        None,
        "--codex-bin",
        help=_CODEX_BIN_OPTION_HELP,
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Optional Codex model override for the managed app-server.",
    ),
    model_reasoning_effort: str | None = typer.Option(
        None,
        "--model-reasoning-effort",
        help="Optional Codex reasoning effort override for the managed app-server.",
    ),
    bypass_approvals: bool = typer.Option(
        False,
        "--dangerously-bypass-approvals-and-sandbox",
        help="Pass --dangerously-bypass-approvals-and-sandbox to the Codex TUI. Opt-in only.",
    ),
) -> None:
    """Launch a Longhouse Codex session on this machine via the Longhouse API."""

    if ctx.invoked_subcommand:
        return

    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = _load_api_credentials(
        url=url,
        token=token,
        config_dir=resolved_config_dir,
        exit_code=managed_local_cli.EXIT_SETUP_FAILED,
    )
    resolved_codex_bin = _resolve_codex_binary(codex_bin)
    if not resolved_codex_bin:
        typer.secho(
            "Codex executable not found. Install the OpenAI Codex CLI so `codex` is on PATH, "
            f"or set {CODEX_BIN_ENV} / --codex-bin explicitly for debugging.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    machine_name = get_machine_name_label()
    _ensure_managed_launch_preflight(
        url=resolved_url,
        machine_name=machine_name,
        config_dir=resolved_config_dir,
        exit_code=managed_local_cli.EXIT_SETUP_FAILED,
    )
    typer.echo(f"Longhouse: {resolved_url}")
    result = _launch_managed_local_from_api(
        url=resolved_url,
        token=resolved_token,
        cwd=cwd,
        project=project,
        loop_mode=loop_mode,
        name=name,
        machine_name=machine_name,
    )
    session_url = _build_session_url(resolved_url, result.session_id)
    typer.secho("Longhouse Codex session launched on this machine.", fg=typer.colors.GREEN)
    typer.echo(f"Session ID: {result.session_id}")
    typer.echo(f"Session URL: {session_url}")
    _emit_warp_cli_agent_event(
        event="session_start",
        session_id=result.session_id,
        cwd=cwd,
        project=project,
    )
    typer.echo("Starting native Codex bridge...")
    try:
        thread_id, ws_url, state_file = _start_native_codex_bridge(
            session_id=result.session_id,
            cwd=cwd,
            url=resolved_url,
            token=resolved_token,
            codex_bin=resolved_codex_bin,
            model=model,
            model_reasoning_effort=model_reasoning_effort,
            create_initial_thread=True,
        )
    except _NativeBridgeError as exc:
        typer.secho(f"Codex bridge failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if thread_id:
        typer.echo(f"Codex thread: {thread_id}")
    typer.echo(f"Remote target: {ws_url}")

    if open_browser:
        typer.echo("Opening session in browser...")
        if not _open_session_url(session_url):
            typer.secho(f"Could not open browser automatically. Visit: {session_url}", fg=typer.colors.YELLOW)

    attach_cmd = _build_codex_attach_command(
        codex_bin=resolved_codex_bin,
        ws_url=ws_url,
        bypass_approvals=bypass_approvals,
        model=model,
        model_reasoning_effort=model_reasoning_effort,
        session_id=result.session_id,
        thread_id=thread_id or None,
    )
    if not attach:
        typer.echo(f"Attach: {attach_cmd}")
        return
    if not _interactive_stdio():
        typer.secho("Skipping auto-attach because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        typer.echo(f"Attach: {attach_cmd}")
        return

    typer.echo("Attaching...")
    bridge_stopper = _CodexBridgeStopper(result.session_id, state_file=state_file)
    previous_handlers = _install_codex_signal_cleanup(bridge_stopper)
    try:
        exit_code = _run_native_codex_tui(
            session_id=result.session_id,
            codex_bin=resolved_codex_bin,
            ws_url=ws_url,
            cwd=cwd,
            bypass_approvals=bypass_approvals,
            model=model,
            model_reasoning_effort=model_reasoning_effort,
            thread_id=thread_id or None,
        )
    finally:
        _restore_signal_handlers(previous_handlers)
    keep_bridge_alive = exit_code != 0 and _active_turn_survived_tui_exit(state_file)
    stop_error = None if keep_bridge_alive else bridge_stopper.stop(reason=_CODEX_STOP_REASON_TERMINAL_DISCONNECTED)
    if exit_code != 0:
        if keep_bridge_alive:
            attach_thread_id = ""
            state = _load_native_codex_bridge_state(state_file)
            if state is not None:
                attach_thread_id = str(state.get("thread_id") or "").strip()
            attach_cmd = _build_codex_attach_command(
                codex_bin=resolved_codex_bin,
                ws_url=ws_url,
                bypass_approvals=bypass_approvals,
                model=model,
                model_reasoning_effort=model_reasoning_effort,
                session_id=result.session_id,
                thread_id=attach_thread_id or None,
            )
            typer.secho(
                "Auto-attach exited, but the managed Codex session is still running and reattachable.",
                fg=typer.colors.YELLOW,
            )
            typer.echo(f"Attach: {attach_cmd}")
            return
        _emit_warp_cli_agent_event(
            event="stop",
            session_id=result.session_id,
            cwd=cwd,
            project=project,
            response=(
                f"Managed Codex auto-attach exited with code {exit_code}; "
                + ("bridge cleanup completed." if stop_error is None else f"bridge cleanup failed: {stop_error}")
            ),
        )
        typer.secho(
            f"Auto-attach exited with code {exit_code}. Managed bridge cleanup was "
            + ("successful." if stop_error is None else f"not successful: {stop_error}"),
            fg=typer.colors.YELLOW,
        )
    elif stop_error is not None:
        _emit_warp_cli_agent_event(
            event="stop",
            session_id=result.session_id,
            cwd=cwd,
            project=project,
            response=f"Managed Codex bridge cleanup failed after TUI exit: {stop_error}",
        )
        typer.secho(
            f"Managed bridge cleanup failed after TUI exit: {stop_error}",
            fg=typer.colors.YELLOW,
        )
    else:
        _emit_warp_cli_agent_event(
            event="stop",
            session_id=result.session_id,
            cwd=cwd,
            project=project,
            response="Managed Codex session ended.",
        )


@app.command("doctor")
def codex_doctor(
    codex_bin: str | None = typer.Option(
        None,
        "--codex-bin",
        help=_CODEX_DOCTOR_BIN_OPTION_HELP,
    ),
    state_root: Path | None = typer.Option(
        None,
        "--state-root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Codex bridge state root override (default: ~/.claude/managed-local/codex-bridge).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    check_readyz: bool = typer.Option(
        False,
        "--check-readyz",
        help="Actively probe each bridge relay readyz endpoint. Off by default to keep doctor fast.",
    ),
) -> None:
    """Inspect the managed Codex binary, legacy artifacts, and bridge state."""

    try:
        payload = _collect_codex_doctor(codex_bin=codex_bin, state_root=state_root, check_readyz=check_readyz)
    except _NativeBridgeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)
    _render_codex_doctor(payload, json_output=json_output)
