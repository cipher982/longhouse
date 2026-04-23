"""Longhouse Codex session launcher CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
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
from zerg.cli._common import interactive_stdio as _interactive_stdio
from zerg.cli._common import load_api_credentials as _load_api_credentials
from zerg.cli._common import open_session_url as _open_session_url
from zerg.services.runtime_artifacts import RuntimeComponent
from zerg.services.runtime_artifacts import resolve_installed_runtime_artifact
from zerg.services.session_continuity import get_machine_name_label
from zerg.services.shipper.service import get_engine_executable
from zerg.session_loop_mode import SessionLoopMode

_CODEX_BIN_ENV = "LONGHOUSE_CODEX_BIN"
_MANAGED_CODEX_CONFIG_OVERRIDE = "check_for_update_on_startup=false"
_ROLLOUT_TURN_EVENT_TYPES = {"task_started", "task_complete", "turn_aborted"}
_ROLLOUT_TERMINAL_EVENT_TYPES = {"task_complete", "turn_aborted"}
_ROLLOUT_TAIL_LINES = 256


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
    normalized = str(explicit or "").strip()
    if normalized:
        return _resolve_explicit_codex_binary(normalized, source="--codex-bin")
    env_candidate = str(os.environ.get(_CODEX_BIN_ENV) or "").strip()
    if env_candidate:
        return _resolve_explicit_codex_binary(env_candidate, source=_CODEX_BIN_ENV)
    managed_runtime = resolve_installed_runtime_artifact(RuntimeComponent.MANAGED_CODEX)
    if managed_runtime is not None:
        return managed_runtime.launch_path
    return None


def _build_codex_attach_command(
    *,
    codex_bin: str,
    ws_url: str,
    bypass_approvals: bool,
    session_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    cmd = [codex_bin, "-c", _MANAGED_CODEX_CONFIG_OVERRIDE]
    if thread_id:
        cmd += ["resume", thread_id]
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
) -> tuple[str, str, str | None]:
    try:
        engine = get_engine_executable()
    except RuntimeError as exc:
        raise _NativeBridgeError(str(exc)) from exc
    completed = subprocess.run(
        [
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
            "--json",
        ],
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
    # thread_id may be empty at launch — the TUI creates the thread after attaching.
    thread_id = str(payload.get("thread_id") or "").strip()
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


def _stop_native_codex_bridge(*, session_id: str) -> str | None:
    try:
        engine = get_engine_executable()
    except RuntimeError as exc:
        return str(exc)
    completed = subprocess.run(
        [
            engine,
            "codex-bridge",
            "stop",
            "--session-id",
            session_id,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return None
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    return stderr or stdout or f"codex-bridge stop exited with code {completed.returncode}"


def _run_native_codex_tui(
    *,
    session_id: str,
    codex_bin: str,
    ws_url: str,
    cwd: Path,
    bypass_approvals: bool = False,
) -> int:
    # Connect TUI to the bridge's app-server. The TUI calls thread/start which
    # creates the thread; the bridge daemon observes the thread/started notification
    # and posts idle once it knows which thread to drive.
    cmd = [codex_bin, "-c", _MANAGED_CODEX_CONFIG_OVERRIDE]
    if bypass_approvals:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd += ["--enable", "tui_app_server", "--remote", ws_url]
    env = os.environ.copy()
    env["LONGHOUSE_MANAGED_SESSION_ID"] = session_id
    completed = subprocess.run(
        cmd,
        check=False,
        cwd=str(cwd),
        env=env,
    )
    return int(completed.returncode)


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


def codex(
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
        SessionLoopMode.MANUAL,
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
        help=f"Codex executable override for managed sessions (defaults to {_CODEX_BIN_ENV} or the installed managed runtime).",
    ),
    bypass_approvals: bool = typer.Option(
        False,
        "--dangerously-bypass-approvals-and-sandbox",
        help="Pass --dangerously-bypass-approvals-and-sandbox to the Codex TUI. Opt-in only.",
    ),
) -> None:
    """Launch a Longhouse Codex session on this machine via the Longhouse API."""

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
            "Managed Codex runtime is not installed yet. Run `longhouse onboard` to complete setup, "
            "`longhouse connect --install` if you've already onboarded, or set "
            f"{_CODEX_BIN_ENV} / --codex-bin to override it explicitly.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    machine_name = get_machine_name_label()
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
    typer.echo("Starting native Codex bridge...")
    try:
        thread_id, ws_url, state_file = _start_native_codex_bridge(
            session_id=result.session_id,
            cwd=cwd,
            url=resolved_url,
            token=resolved_token,
            codex_bin=resolved_codex_bin,
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
        session_id=result.session_id,
    )
    if not attach:
        typer.echo(f"Attach: {attach_cmd}")
        return
    if not _interactive_stdio():
        typer.secho("Skipping auto-attach because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        typer.echo(f"Attach: {attach_cmd}")
        return

    typer.echo("Attaching...")
    exit_code = _run_native_codex_tui(
        session_id=result.session_id,
        codex_bin=resolved_codex_bin,
        ws_url=ws_url,
        cwd=cwd,
        bypass_approvals=bypass_approvals,
    )
    keep_bridge_alive = exit_code != 0 and _active_turn_survived_tui_exit(state_file)
    stop_error = None if keep_bridge_alive else _stop_native_codex_bridge(session_id=result.session_id)
    if exit_code != 0:
        if keep_bridge_alive:
            resume_thread_id = ""
            state = _load_native_codex_bridge_state(state_file)
            if state is not None:
                resume_thread_id = str(state.get("thread_id") or "").strip()
            resume_cmd = _build_codex_attach_command(
                codex_bin=resolved_codex_bin,
                ws_url=ws_url,
                bypass_approvals=bypass_approvals,
                session_id=result.session_id,
                thread_id=resume_thread_id or None,
            )
            typer.secho(
                "Auto-attach exited, but the managed Codex session is still running and resumable.",
                fg=typer.colors.YELLOW,
            )
            typer.echo(f"Resume: {resume_cmd}")
            return
        typer.secho(
            f"Auto-attach exited with code {exit_code}. Managed bridge cleanup was "
            + ("successful." if stop_error is None else f"not successful: {stop_error}"),
            fg=typer.colors.YELLOW,
        )
    elif stop_error is not None:
        typer.secho(
            f"Managed bridge cleanup failed after TUI exit: {stop_error}",
            fg=typer.colors.YELLOW,
        )
