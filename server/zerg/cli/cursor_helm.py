"""Longhouse Cursor Helm launcher — invisible interactive TUI + remote steer.

``longhouse cursor`` runs ``cursor-agent`` under a pseudo-terminal and pipes
bytes through transparently (no tmux, no terminal emulation — the TUI is
pristine). Longhouse OWNS the PTY master and the cursor-agent child: that
ownership is what makes remote steer possible. The launcher:

- registers a managed-local session with the Runtime Host
  (``/api/sessions/managed-local/this-device``);
- binds a per-session Unix domain socket and writes a private state file under
  ``~/.longhouse/managed-local/cursor-helm/`` that the Machine Agent scans into
  a heartbeat lease (so the UI shows the session live + steerable);
- forwards remote ``send`` / ``interrupt`` / ``terminate`` commands received on
  that socket to the PTY master / child pid:
  - send:   ``text -> 0.3s -> Escape -> 0.1s -> Enter`` (Ink submit workaround,
    claude-code#15553);
  - interrupt: ``SIGINT`` to the cursor-agent child (#52812);
  - terminate: ``SIGKILL`` the child, then cleanup + exit.

The engine connects to the socket per command; see
``engine/src/cursor_helm_control.rs``. The launcher is the only process that
can inject terminal input (it holds the PTY master fd) and it owns the child
pid for signaling — engine restart only pauses remote control; the local TUI
keeps running.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx
import typer

from zerg.cli import _launch_ui as launch_ui
from zerg.cli._common import build_session_url
from zerg.cli._common import ensure_managed_launch_preflight
from zerg.cli._common import git_output
from zerg.cli._common import interactive_stdio
from zerg.cli._common import load_api_credentials
from zerg.cli._managed_launch import interactive_human_shell_launch_provenance
from zerg.services.longhouse_paths import get_managed_local_dir
from zerg.services.machine_identity import get_machine_name_label
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.session_loop_mode import SessionLoopMode

EXIT_SETUP_FAILED = 78
EXIT_NOT_INTERACTIVE = 79
_CURSOR_BIN_ENV = "LONGHOUSE_CURSOR_BIN"
_CURSOR_BIN_DEFAULT = "cursor-agent"


# Ink (cursor-agent's TUI) intercepts a programmatic Enter as autocomplete and
# swallows the submit. Escape dismisses the autocomplete popup, then Enter
# submits. Validated live in the PTY pass-through spike. The settle delays are
# tunable via env so dogfooding can adjust them without a rebuild:
#   LH_CURSOR_HELM_TEXT_SETTLE_MS    (default 300)
#   LH_CURSOR_HELM_ESCAPE_SETTLE_MS  (default 100)
def _env_seconds(name: str, default_ms: int) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default_ms / 1000.0
    try:
        return max(0.0, int(raw)) / 1000.0
    except ValueError:
        return default_ms / 1000.0


_INJECT_TEXT_SETTLE_SECONDS = _env_seconds("LH_CURSOR_HELM_TEXT_SETTLE_MS", 300)
_INJECT_ESCAPE_SETTLE_SECONDS = _env_seconds("LH_CURSOR_HELM_ESCAPE_SETTLE_MS", 100)
_SOCKET_BACKLOG = 4
_COMMAND_READ_TIMEOUT = 8.0
# Keep short so Helm exit can join an in-flight attempt without wedging for 30s.
_REGISTER_TIMEOUT = 5.0
_REGISTER_RETRY_DELAYS_SECONDS = (0.0, 0.5, 1.5, 3.0)
_REGISTER_EXIT_JOIN_SECONDS = _REGISTER_TIMEOUT + 1.0
_TERMINAL_POST_TIMEOUT = 5.0
_PROVIDER = "cursor"
_CONTROL_PLANE = "cursor_helm"
_STATE_PROVIDER_DIR = "cursor-helm"


@dataclass(frozen=True)
class _RegistrationOutcome:
    session_id: str
    registered: bool
    attach_command: str = ""
    error: str | None = None


def _panel_capability_for_registration(outcome: _RegistrationOutcome | None) -> str:
    """Map registration outcome to honest launch-panel capability.

    Soft-fail must never advertise steerable remote control.
    """
    if outcome is None:
        return "registering"
    if outcome.registered:
        return "steerable"
    return "local_only"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir() -> Path:
    return get_managed_local_dir(_STATE_PROVIDER_DIR)


def _state_file_path(session_id: str) -> Path:
    return _state_dir() / f"{session_id}.json"


def _socket_path(session_id: str) -> Path:
    return _state_dir() / f"{session_id}.sock"


def _process_start_time(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        value = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return value or None


def _write_state(
    session_id: str,
    *,
    socket_path: Path,
    cursor_pid: int,
    cwd: Path,
    ready: bool,
    registration: str = "pending",
    registration_error: str | None = None,
) -> None:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    started_at = _now_iso()
    existing_path = _state_file_path(session_id)
    existing: dict = {}
    try:
        loaded = json.loads(existing_path.read_text())
        existing = loaded if isinstance(loaded, dict) else {}
        if isinstance(existing.get("started_at"), str) and existing["started_at"].strip():
            started_at = existing["started_at"]
    except (OSError, ValueError, TypeError):
        existing = {}
    launcher_pid = os.getpid()
    launcher_process_start_time = (
        existing.get("launcher_process_start_time") if existing.get("launcher_pid") == launcher_pid else None
    ) or _process_start_time(launcher_pid)
    if not launcher_process_start_time:
        raise RuntimeError("could not capture Cursor Helm launcher process identity")
    cursor_process_start_time = None
    if cursor_pid > 0:
        cursor_process_start_time = (
            existing.get("cursor_process_start_time") if existing.get("cursor_pid") == cursor_pid else None
        ) or _process_start_time(cursor_pid)
        if not cursor_process_start_time:
            raise RuntimeError("could not capture cursor-agent process identity")
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "provider": _PROVIDER,
        "control_plane": _CONTROL_PLANE,
        "socket_path": str(socket_path),
        "launcher_pid": launcher_pid,
        "launcher_process_start_time": launcher_process_start_time,
        "cursor_pid": cursor_pid,
        "cursor_process_start_time": cursor_process_start_time,
        "cwd": str(cwd),
        "ready": ready,
        "registration": registration,
        "registration_error": registration_error,
        "started_at": started_at,
        "updated_at": _now_iso(),
    }
    tmp = _state_file_path(session_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, _state_file_path(session_id))


def _remove_state(session_id: str, socket_path: Path) -> None:
    try:
        if socket_path.exists():
            socket_path.unlink()
    except OSError:
        pass
    try:
        _state_file_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _resolve_cursor_bin() -> str:
    configured = os.environ.get(_CURSOR_BIN_ENV, "").strip()
    if configured:
        return configured
    found = shutil.which(_CURSOR_BIN_DEFAULT)
    if not found:
        typer.secho(
            f"`{_CURSOR_BIN_DEFAULT}` not found on PATH. Install Cursor's CLI or set {_CURSOR_BIN_ENV}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    return found


def _infer_git_context(cwd: Path) -> tuple[str | None, str | None]:
    repo = git_output(cwd, "config", "--get", "remote.origin.url")
    branch = git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        branch = None
    return (repo or None, branch or None)


def _register_session(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    name: str | None,
    loop_mode: SessionLoopMode,
    machine_name: str,
    permission_mode: str,
    session_id: str,
    verbose: bool = False,
) -> _RegistrationOutcome:
    """Attempt host registration. Remote failures return degraded outcome; do not exit."""

    git_repo, git_branch = _infer_git_context(cwd)
    payload = {
        "cwd": str(cwd),
        "provider": _PROVIDER,
        "project": project,
        "git_repo": git_repo,
        "git_branch": git_branch,
        "display_name": name,
        "loop_mode": loop_mode.value,
        "machine_name": machine_name,
        "permission_mode": permission_mode,
        "session_id": session_id,
    }
    launch_actor, launch_surface = interactive_human_shell_launch_provenance()
    if launch_actor:
        payload["launch_actor"] = launch_actor
    if launch_surface:
        payload["launch_surface"] = launch_surface
    launch_url = f"{url.rstrip('/')}/api/sessions/managed-local/this-device"
    if verbose:
        typer.echo(f"Creating Longhouse managed cursor session: POST {launch_url}")
    try:
        with httpx.Client(timeout=_REGISTER_TIMEOUT) as client:
            response = client.post(launch_url, headers={"X-Agents-Token": token}, json=payload)
    except httpx.ConnectError as exc:
        return _RegistrationOutcome(session_id=session_id, registered=False, error=f"connect failed: {exc}")
    except httpx.TimeoutException:
        return _RegistrationOutcome(session_id=session_id, registered=False, error="registration timed out")

    if response.status_code == 401:
        # Soft-fail: Degraded Helm still runs the local TUI; do not Exit from a
        # background registration thread (typer.Exit would not stop the launcher).
        return _RegistrationOutcome(
            session_id=session_id,
            registered=False,
            error="authentication failed; run 'longhouse auth' to re-authenticate",
        )
    if response.status_code == 422:
        try:
            errors = response.json()
        except ValueError:
            errors = response.text[:200]
        return _RegistrationOutcome(
            session_id=session_id,
            registered=False,
            error=f"server rejected launch (422): {errors}",
        )
    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("detail") or "").strip()
        except ValueError:
            detail = response.text.strip()
        return _RegistrationOutcome(
            session_id=session_id,
            registered=False,
            error=detail or f"registration failed HTTP {response.status_code}",
        )

    body = response.json()
    returned_id = str(body.get("session_id") or "").strip()
    if returned_id and returned_id != session_id:
        return _RegistrationOutcome(
            session_id=session_id,
            registered=False,
            error=f"server returned different session_id {returned_id}",
        )
    return _RegistrationOutcome(
        session_id=session_id,
        registered=True,
        attach_command=str(body.get("attach_command") or ""),
    )


def _registration_worker(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    name: str | None,
    loop_mode: SessionLoopMode,
    machine_name: str,
    permission_mode: str,
    session_id: str,
    sock_path: Path,
    stop_event: threading.Event,
    outcome_box: list[_RegistrationOutcome],
    outcome_lock: threading.Lock,
    verbose: bool,
) -> None:
    last_error: str | None = None
    for delay in _REGISTER_RETRY_DELAYS_SECONDS:
        if stop_event.is_set():
            return
        if delay:
            stop_event.wait(delay)
            if stop_event.is_set():
                return
        outcome = _register_session(
            url=url,
            token=token,
            cwd=cwd,
            project=project,
            name=name,
            loop_mode=loop_mode,
            machine_name=machine_name,
            permission_mode=permission_mode,
            session_id=session_id,
            verbose=verbose,
        )
        if stop_event.is_set():
            if outcome.registered:
                # Host materialized after local exit — terminalize immediately.
                _post_terminal_event(url, token, session_id, "helm_exit_before_ready")
            return
        if outcome.registered:
            with outcome_lock:
                outcome_box[:] = [outcome]
            try:
                current = json.loads(_state_file_path(session_id).read_text())
                _write_state(
                    session_id,
                    socket_path=sock_path,
                    cursor_pid=int(current.get("cursor_pid") or 0),
                    cwd=cwd,
                    ready=bool(current.get("ready")),
                    registration="registered",
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                _write_state(
                    session_id,
                    socket_path=sock_path,
                    cursor_pid=0,
                    cwd=cwd,
                    ready=False,
                    registration="registered",
                )
            return
        last_error = outcome.error
    with outcome_lock:
        outcome_box[:] = [_RegistrationOutcome(session_id=session_id, registered=False, error=last_error or "registration failed")]
    try:
        current = json.loads(_state_file_path(session_id).read_text())
        _write_state(
            session_id,
            socket_path=sock_path,
            cursor_pid=int(current.get("cursor_pid") or 0),
            cwd=cwd,
            ready=bool(current.get("ready")),
            registration="degraded",
            registration_error=last_error,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _write_state(
            session_id,
            socket_path=sock_path,
            cursor_pid=0,
            cwd=cwd,
            ready=False,
            registration="degraded",
            registration_error=last_error,
        )


def _post_terminal_event(url: str, token: str, session_id: str, reason: str) -> None:
    """Best-effort: tell the Runtime Host the Helm session ended."""
    occurred_at = _now_iso()
    device_id = get_machine_name_label()
    event = {
        "runtime_key": f"{_PROVIDER}:{session_id}",
        "session_id": session_id,
        "provider": _PROVIDER,
        "device_id": device_id,
        "source": "cursor_helm",
        "kind": "terminal_signal",
        "phase": "finished",
        "occurred_at": occurred_at,
        "dedupe_key": f"cursor-helm-terminal:{session_id}:{reason}:{occurred_at}",
        "payload": {
            "terminal_state": "session_ended",
            "terminal_reason": reason,
            "terminal_source": "cursor_helm",
            "exit_code": 0,
        },
    }
    endpoint = f"{url.rstrip('/')}/api/agents/runtime/events/batch"
    try:
        with httpx.Client(timeout=_TERMINAL_POST_TIMEOUT) as client:
            client.post(
                endpoint,
                headers={"X-Agents-Token": token},
                json={"events": [event]},
            )
    except httpx.HTTPError:
        pass


def _reconcile_registration_on_exit(
    *,
    url: str,
    token: str,
    session_id: str,
    registration_thread: threading.Thread,
    registration_box: list[_RegistrationOutcome],
    registration_lock: threading.Lock,
    join_timeout: float = _REGISTER_EXIT_JOIN_SECONDS,
) -> bool:
    """Join registration briefly and close any host session that may exist.

    Returns True when registration succeeded (durable exit copy).
    If the outcome is still unknown after the bounded join, best-effort
    terminalize so a late host commit cannot linger as falsely live.
    """
    registration_thread.join(timeout=join_timeout)
    with registration_lock:
        outcome = registration_box[0] if registration_box else None
    if outcome is not None and outcome.registered:
        _post_terminal_event(url, token, session_id, "helm_exit")
        return True
    if outcome is None:
        # Abandoned / killed mid-HTTP — host may have committed after our join.
        _post_terminal_event(url, token, session_id, "helm_exit_before_ready")
    return False


def _set_window_title(text: str) -> None:
    try:
        sys.stderr.write(f"\x1b]0;{text}\x07")
        sys.stderr.flush()
    except Exception:
        pass


def _get_terminal_size(fd: int) -> tuple[int, int]:
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols, _, _ = struct.unpack("hhhh", packed)
        return rows, cols
    except Exception:
        return (24, 80)


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass


def _full_write(fd: int, data: bytes) -> int:
    """Write all bytes to fd, looping past partial writes. Raises OSError on a
    terminal write failure (caller decides whether that ends the session)."""
    total = 0
    while data:
        n = os.write(fd, data)
        total += n
        data = data[n:]
    return total


def _inject_send(master_fd: int, text: str, lock: threading.Lock) -> None:
    data = text.encode("utf-8", errors="replace")
    with lock:
        _full_write(master_fd, data)
        time.sleep(_INJECT_TEXT_SETTLE_SECONDS)
        _full_write(master_fd, b"\x1b")  # Escape dismisses Ink autocomplete popup
        time.sleep(_INJECT_ESCAPE_SETTLE_SECONDS)
        _full_write(master_fd, b"\r")  # Enter submits


def _handle_command(
    request: dict,
    *,
    master_fd: int,
    child_pid: int,
    master_lock: threading.Lock,
    stop_event: threading.Event,
) -> dict:
    kind = str(request.get("kind") or "").strip()
    if kind == "send":
        text = str(request.get("text") or "")
        if not text:
            return {"ok": False, "error": {"code": "bad_request", "message": "missing text"}}
        try:
            _inject_send(master_fd, text, master_lock)
        except OSError as exc:
            # PTY master is closed — the cursor-agent child has exited. Report
            # not-attached so the engine/UI marks the session gone instead of
            # retrying or labeling it a transient command failure.
            return {
                "ok": False,
                "error": {"code": "session_not_attached", "message": f"pty closed: {exc}"},
            }
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    if kind == "interrupt":
        try:
            os.kill(child_pid, signal.SIGINT)
        except ProcessLookupError:
            return {"ok": False, "error": {"code": "session_not_attached", "message": "child gone"}}
        except OSError as exc:
            return {"ok": False, "error": {"code": "command_failed", "message": str(exc)}}
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    if kind == "terminate":
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        stop_event.set()
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    if kind == "ping":
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    return {"ok": False, "error": {"code": "bad_request", "message": f"unknown kind {kind!r}"}}


def _socket_server(
    sock: socket.socket,
    *,
    master_fd: int,
    child_pid: int,
    master_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    sock.settimeout(0.5)
    while not stop_event.is_set():
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            _serve_one(conn, master_fd=master_fd, child_pid=child_pid, master_lock=master_lock, stop_event=stop_event)
        except Exception:
            try:
                conn.sendall(b'{"ok": false, "error": {"code": "command_failed", "message": "server error"}}\n')
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _serve_one(
    conn: socket.socket,
    *,
    master_fd: int,
    child_pid: int,
    master_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    conn.settimeout(_COMMAND_READ_TIMEOUT)
    buf = bytearray()
    while len(buf) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
    if not buf:
        return
    line, _, _ = bytes(buf).partition(b"\n")
    try:
        request = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        reply = {"ok": False, "error": {"code": "bad_request", "message": "invalid JSON"}}
        conn.sendall((json.dumps(reply) + "\n").encode())
        return
    reply = _handle_command(
        request,
        master_fd=master_fd,
        child_pid=child_pid,
        master_lock=master_lock,
        stop_event=stop_event,
    )
    conn.sendall((json.dumps(reply) + "\n").encode())


def run_helm(
    *,
    cwd: Path,
    project: str | None,
    name: str | None,
    loop_mode: SessionLoopMode,
    url: str | None,
    token: str | None,
    config_dir: str | None,
    permission_mode: str,
    cursor_args: list[str] | None,
    verbose: bool = False,
    open_browser: bool = False,
) -> None:
    if not interactive_stdio():
        typer.secho(
            "longhouse cursor Helm needs an interactive terminal. " "For headless launches use the Longhouse web/iOS Console.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_NOT_INTERACTIVE)

    launch_ui.quiet_diagnostic_logs(verbose)

    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = load_api_credentials(
        url=url,
        token=token,
        config_dir=resolved_config_dir,
        exit_code=EXIT_SETUP_FAILED,
        resolve_url=get_zerg_url,
        resolve_token=load_token,
    )
    machine_name = get_machine_name_label()
    ensure_managed_launch_preflight(
        url=resolved_url,
        machine_name=machine_name,
        config_dir=resolved_config_dir,
        config_dir_is_provider_home=False,
        exit_code=EXIT_SETUP_FAILED,
    )
    cursor_bin = _resolve_cursor_bin()

    launch_ui.progress("Preparing your session…")
    session_id = str(uuid.uuid4())
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    sock_path = _socket_path(session_id)
    _write_state(
        session_id,
        socket_path=sock_path,
        cursor_pid=0,
        cwd=cwd,
        ready=False,
        registration="pending",
    )

    # Bind the control socket before forking. ready=false until the child is
    # running so the engine does not publish a live remote-control lease early.
    try:
        if sock_path.exists():
            sock_path.unlink()
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(str(sock_path))
        server_sock.listen(_SOCKET_BACKLOG)
        os.chmod(sock_path, 0o600)
    except OSError as exc:
        typer.secho(f"Failed to bind cursor-helm control socket: {exc}", fg=typer.colors.RED)
        _remove_state(session_id, sock_path)
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    stop_event = threading.Event()
    registration_box: list[_RegistrationOutcome] = []
    registration_lock = threading.Lock()
    registration_thread = threading.Thread(
        target=_registration_worker,
        kwargs={
            "url": resolved_url,
            "token": resolved_token,
            "cwd": cwd,
            "project": project,
            "name": name,
            "loop_mode": loop_mode,
            "machine_name": machine_name,
            "permission_mode": permission_mode,
            "session_id": session_id,
            "sock_path": sock_path,
            "stop_event": stop_event,
            "outcome_box": registration_box,
            "outcome_lock": registration_lock,
            "verbose": verbose,
        },
        daemon=True,
        name="cursor-helm-register",
    )
    registration_thread.start()
    # Brief race: if registration is fast, print steerable panel; never wait
    # for the full HTTP timeout before starting the TUI.
    registration_thread.join(timeout=0.3)
    with registration_lock:
        early = registration_box[0] if registration_box else None
    panel_capability = _panel_capability_for_registration(early)
    if early is not None and early.registered:
        attach_command = early.attach_command or None
    elif early is not None and not early.registered:
        attach_command = None
        typer.secho(
            f"Warning: Longhouse registration failed ({early.error}). "
            "Continuing local Cursor Helm; remote steer/timeline may be unavailable.",
            fg=typer.colors.YELLOW,
        )
    else:
        attach_command = None

    if verbose:
        typer.echo(f"Longhouse: {resolved_url}")
        typer.echo(f"Session:   {session_id}")
        typer.echo(f"Timeline:  {build_session_url(resolved_url, session_id)}")

    # Ownership signal: set the terminal window title. The TUI's alternate
    # screen would clear a printed banner; the title persists.
    _set_window_title("Longhouse Helm · cursor-agent")

    typer.secho(
        "Cursor Helm remote control is live when Longhouse registration and the machine lease succeed. "
        "Transcript archive is unavailable until the native Cursor storage-v2 source is enabled.",
        fg=typer.colors.YELLOW,
    )

    launch_ui.launch_panel(
        provider_label=launch_ui.PROVIDER_LABELS["cursor"],
        base_url=resolved_url,
        machine_name=machine_name,
        session_id=session_id,
        verbose=verbose,
        capability=panel_capability,
        attach_command=attach_command,
    )

    argv = [cursor_bin, *(cursor_args or [])]

    # Read the real terminal's mode + geometry before forking. We need the size
    # in two places: to preseed LINES/COLUMNS in the child env (mitigates the
    # startup race where cursor-agent samples the PTY's 0x0 winsize before our
    # TIOCSWINSZ lands) and to set the PTY winsize immediately after fork.
    real_stdin = sys.stdin.fileno()
    real_stdout = sys.stdout.fileno()
    saved_term = termios.tcgetattr(real_stdin)
    real_rows, real_cols = _get_terminal_size(real_stdout)

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: cursor-agent under the PTY slave.
        try:
            os.chdir(str(cwd))
        except OSError:
            pass
        env = dict(os.environ)
        env.setdefault("LONGHOUSE_SESSION_ID", session_id)
        # Ink (cursor-agent's TUI) disables ANSI erase/cursor manipulation,
        # synchronized output, and SIGWINCH resize handling when it detects a
        # CI environment or when stdout is not a TTY. The child's stdout is the
        # PTY slave (a TTY), but CI-detection vars inherited from the parent
        # shell would still flip it into non-interactive mode and silently
        # break the render. Strip the common CI sentinels (the set ci-info /
        # Ink detect) and guarantee a real TERM so Ink stays interactive. Leave
        # user color prefs (NO_COLOR etc.) untouched.
        for _ci_var in (
            "CI",
            "CONTINUOUS_INTEGRATION",
            "GITHUB_ACTIONS",
            "GITLAB_CI",
            "CIRCLECI",
            "TRAVIS",
            "BUILDKITE",
            "TEAMCITY_VERSION",
            "BUILD_NUMBER",
            "BUILD_ID",
            "BITBUCKET_BUILD_NUMBER",
            "JENKINS_URL",
        ):
            env.pop(_ci_var, None)
        if not env.get("TERM") or env.get("TERM") == "dumb":
            env["TERM"] = "xterm-256color"
        # Best-effort guard against the startup winsize race. The kernel also
        # delivers SIGWINCH once the parent sets the PTY size, but preseeding
        # LINES/COLUMNS covers cursor-agent's first frame if it samples before
        # that lands.
        env["LINES"] = str(real_rows)
        env["COLUMNS"] = str(real_cols)
        try:
            os.execvpe(argv[0], argv, env)
        except OSError as exc:
            sys.stderr.write(f"longhouse cursor: failed to exec {argv[0]}: {exc}\n")
            os._exit(127)

    # Parent: own the PTY master + child pid. ready=true publishes the live lease.
    registration_status = "pending"
    registration_error: str | None = None
    with registration_lock:
        if registration_box and registration_box[0].registered:
            registration_status = "registered"
        elif registration_box and not registration_box[0].registered:
            registration_status = "degraded"
            registration_error = registration_box[0].error
    try:
        current = json.loads(_state_file_path(session_id).read_text())
        if registration_status == "pending":
            registration_status = str(current.get("registration") or "pending")
            err = current.get("registration_error")
            registration_error = err if isinstance(err, str) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    _write_state(
        session_id,
        socket_path=sock_path,
        cursor_pid=pid,
        cwd=cwd,
        ready=True,
        registration=registration_status,
        registration_error=registration_error,
    )
    _set_pty_size(master_fd, real_rows, real_cols)

    master_lock = threading.Lock()

    def _on_winch(*_args: object) -> None:
        rows, cols = _get_terminal_size(real_stdout)
        _set_pty_size(master_fd, rows, cols)

    prev_winch = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, _on_winch)

    server_thread = threading.Thread(
        target=_socket_server,
        args=(server_sock,),
        kwargs={
            "master_fd": master_fd,
            "child_pid": pid,
            "master_lock": master_lock,
            "stop_event": stop_event,
        },
        daemon=True,
        name="cursor-helm-socket",
    )
    server_thread.start()

    try:
        tty.setraw(real_stdin)
    except termios.error as exc:
        # A cooked terminal breaks the cursor-agent TUI render (escape sequences
        # mangled, input line-buffered). Kill the child, let the finally below
        # restore the terminal + close fds, and bail with a clear error rather
        # than silently running a mangled session.
        typer.secho(
            f"longhouse cursor: cannot set terminal to raw mode: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stop_event.set()

    exit_code = 0
    try:
        while not stop_event.is_set():
            try:
                readable, _, _ = select.select([real_stdin, master_fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if real_stdin in readable:
                try:
                    data = os.read(real_stdin, 4096)
                except OSError:
                    data = b""
                if not data:
                    # stdin closed (Ctrl-D) — forward EOF to the child.
                    try:
                        _full_write(master_fd, b"\x04")
                    except OSError:
                        stop_event.set()
                else:
                    try:
                        _full_write(master_fd, data)
                    except OSError:
                        stop_event.set()
                        break
            if master_fd in readable:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    # child exited / closed the PTY
                    stop_event.set()
                    break
                try:
                    _full_write(real_stdout, data)
                except OSError:
                    stop_event.set()
                    break
        # Reap the child to get its exit code.
        try:
            _, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                exit_code = 128 + os.WTERMSIG(status)
        except ChildProcessError:
            pass
    finally:
        stop_event.set()
        # Bounded join covers one in-flight register attempt; unknown outcomes
        # still get a best-effort terminalize (see _reconcile_registration_on_exit).
        _reconcile_registration_on_exit(
            url=resolved_url,
            token=resolved_token,
            session_id=session_id,
            registration_thread=registration_thread,
            registration_box=registration_box,
            registration_lock=registration_lock,
        )
        try:
            termios.tcsetattr(real_stdin, termios.TCSADRAIN, saved_term)
        except termios.error:
            pass
        try:
            signal.signal(signal.SIGWINCH, prev_winch)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            server_sock.close()
        except OSError:
            pass
        _remove_state(session_id, sock_path)
        # Catalog registration proves remote control ownership, not transcript
        # durability. Cursor stays explicitly control-only until storage-v2
        # receipts exist for its native source.
        launch_ui.exit_bookend(exit_code=exit_code, machine_name=machine_name, durable=False)
        if open_browser:
            typer.echo(f"Timeline: {build_session_url(resolved_url, session_id)}")

    raise typer.Exit(code=exit_code)
