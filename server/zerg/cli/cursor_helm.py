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
import sys
import termios
import threading
import time
import tty
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx
import typer

from zerg.cli import _launch_ui as launch_ui
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli._common import build_session_url
from zerg.cli._common import ensure_managed_launch_preflight
from zerg.cli._common import git_output
from zerg.cli._common import interactive_stdio
from zerg.cli._common import load_api_credentials
from zerg.cli.cursor_helm_ingest import probe_ingest_path
from zerg.cli.cursor_helm_ingest import run_helm_ingest_thread
from zerg.services.longhouse_paths import get_managed_local_dir
from zerg.services.machine_identity import get_machine_name_label
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.session_loop_mode import SessionLoopMode
from zerg.utils.log import BestEffortLogger

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
_REGISTER_TIMEOUT = 30.0
_TERMINAL_POST_TIMEOUT = 5.0
_PROVIDER = "cursor"
_CONTROL_PLANE = "cursor_helm"
_STATE_PROVIDER_DIR = "cursor-helm"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir() -> Path:
    return get_managed_local_dir(_STATE_PROVIDER_DIR)


def _state_file_path(session_id: str) -> Path:
    return _state_dir() / f"{session_id}.json"


def _socket_path(session_id: str) -> Path:
    return _state_dir() / f"{session_id}.sock"


def _write_state(
    session_id: str,
    *,
    socket_path: Path,
    cursor_pid: int,
    cwd: Path,
    ready: bool,
) -> None:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "provider": _PROVIDER,
        "control_plane": _CONTROL_PLANE,
        "socket_path": str(socket_path),
        "launcher_pid": os.getpid(),
        "cursor_pid": cursor_pid,
        "cwd": str(cwd),
        "ready": ready,
        "started_at": _now_iso(),
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
    verbose: bool = False,
) -> ManagedLocalLaunchResponse:
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
    }
    launch_url = f"{url.rstrip('/')}/api/sessions/managed-local/this-device"
    if verbose:
        typer.echo(f"Creating Longhouse managed cursor session: POST {launch_url}")
    try:
        with httpx.Client(timeout=_REGISTER_TIMEOUT) as client:
            response = client.post(launch_url, headers={"X-Agents-Token": token}, json=payload)
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    except httpx.TimeoutException:
        typer.secho(
            f"Timed out waiting for Longhouse to create the managed cursor session at {url}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    if response.status_code == 401:
        typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    if response.status_code == 422:
        try:
            errors = response.json()
        except ValueError:
            errors = response.text[:200]
        typer.secho(
            "Longhouse server rejected the launch request (422).\n"
            "Your CLI likely drifted from the server schema. Update with:\n"
            "  cd ~/git/zerg/longhouse && make dogfood-refresh\n"
            f"Server detail: {errors}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("detail") or "").strip()
        except ValueError:
            detail = response.text.strip()
        typer.secho(detail or "Longhouse session launch failed", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    body = response.json()
    raw_provider_session_id = body.get("provider_session_id")
    provider_session_id = str(raw_provider_session_id).strip() if raw_provider_session_id else None
    return ManagedLocalLaunchResponse(
        session_id=str(body["session_id"]),
        provider_session_id=provider_session_id,
        attach_command=str(body["attach_command"]),
        source_runner_name=str(body.get("source_runner_name") or machine_name),
        managed_transport=str(body.get("managed_transport") or "") or None,
        permission_mode=str(body.get("permission_mode") or "bypass"),
    )


def _post_terminal_event(url: str, token: str, session_id: str, reason: str) -> None:
    """Best-effort: tell the Runtime Host the Helm session ended."""
    endpoint = f"{url.rstrip('/')}/api/agents/runtime/event"
    payload = {
        "session_id": session_id,
        "provider": _PROVIDER,
        "kind": "terminal",
        "reason": reason,
        "timestamp": _now_iso(),
    }
    try:
        with httpx.Client(timeout=_TERMINAL_POST_TIMEOUT) as client:
            client.post(endpoint, headers={"X-Agents-Token": token}, json=payload)
    except httpx.HTTPError:
        pass


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
    result = _register_session(
        url=resolved_url,
        token=resolved_token,
        cwd=cwd,
        project=project,
        name=name,
        loop_mode=loop_mode,
        machine_name=machine_name,
        permission_mode=permission_mode,
        verbose=verbose,
    )
    session_id = result.session_id
    if verbose:
        typer.echo(f"Longhouse: {resolved_url}")
        typer.echo(f"Session:   {session_id}")
        typer.echo(f"Timeline:  {build_session_url(resolved_url, session_id)}")

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    sock_path = _socket_path(session_id)
    _write_state(session_id, socket_path=sock_path, cursor_pid=0, cwd=cwd, ready=False)

    # Bind the control socket before forking so the engine can connect as soon
    # as the lease is observed.
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

    # Ownership signal: set the terminal window title. The TUI's alternate
    # screen would clear a printed banner; the title persists.
    _set_window_title("Longhouse Helm · cursor-agent")

    # Launch-time ingest self-check: exercise the exact import + model-build
    # path the tailer uses on every poll. If a transitive import (e.g.
    # zerg.database config validation) would crash without DATABASE_URL, warn
    # NOW — before the alt-screen — so the user knows the session will be
    # steerable but won't appear on the timeline, instead of discovering it
    # from an empty session URL after the first turn.
    ingest_ok, ingest_err = probe_ingest_path()
    if not ingest_ok:
        typer.secho(
            "Warning: live transcript ingest is broken on this machine "
            f"({ingest_err}). The session will be steerable but won't appear "
            "on the timeline until fixed. Run `longhouse machine repair` or "
            "`longhouse upgrade`.",
            fg=typer.colors.YELLOW,
        )

    # Hearth splash: print once the control socket is bound (the steer surface
    # is up, so the "steer from anywhere" claim is honest) and before pty.fork,
    # so it lands on the cooked terminal before cursor-agent's alt-screen
    # clears it. It remains in scrollback and reappears when the TUI exits.
    launch_ui.launch_panel(
        provider_label=launch_ui.PROVIDER_LABELS["cursor"],
        base_url=resolved_url,
        machine_name=machine_name,
        session_id=session_id,
        verbose=verbose,
        steerable=True,
        attach_command=result.attach_command or None,
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

    # Parent: own the PTY master + child pid.
    _write_state(session_id, socket_path=sock_path, cursor_pid=pid, cwd=cwd, ready=True)
    _set_pty_size(master_fd, real_rows, real_cols)

    stop_event = threading.Event()
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

    # Live transcript tailer: stream new turn events from cursor's store.db to
    # the Runtime Host so the Helm session appears on the timeline as turns
    # commit (not just live+steerable). Best-effort daemon thread; see
    # zerg.cli.cursor_helm_ingest + docs/specs/cursor-live-ingest.md.
    ingest_bf = BestEffortLogger("zerg.cursor_helm.ingest")
    launch_time = datetime.now(timezone.utc)
    ingest_thread = threading.Thread(
        target=run_helm_ingest_thread,
        kwargs={
            "launch_time": launch_time,
            "session_id": session_id,
            "url": resolved_url,
            "token": resolved_token,
            "stop_event": stop_event,
            "verbose": verbose,
            "bf": ingest_bf,
        },
        daemon=True,
        name="cursor-helm-ingest",
    )
    ingest_thread.start()

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
        # Let the ingest tailer flush a final poll + record its last outcome
        # before we summarize. Bounded so a hung decode can't wedge exit.
        ingest_thread.join(timeout=5.0)
        ingest_bf.summarize("cursor helm ingest")
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
        _post_terminal_event(resolved_url, resolved_token, session_id, "helm_exit")
        launch_ui.exit_bookend(exit_code=exit_code, machine_name=machine_name)
        if open_browser:
            typer.echo(f"Timeline: {build_session_url(resolved_url, session_id)}")

    raise typer.Exit(code=exit_code)
