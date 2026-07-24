"""Longhouse Claude session launcher CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from datetime import timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import httpx
import typer

from zerg.cli import _launch_ui as launch_ui
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli._common import build_session_url as _build_session_url
from zerg.cli._common import ensure_managed_launch_preflight as _ensure_managed_launch_preflight
from zerg.cli._common import interactive_stdio as _interactive_stdio
from zerg.cli._common import open_session_url as _open_session_url
from zerg.cli._managed_contract import remove_managed_provider_contract
from zerg.cli._managed_launch import EXIT_SETUP_FAILED
from zerg.cli._managed_launch import build_managed_local_launch_payload  # noqa: F401 (re-exported for tests)
from zerg.cli._managed_launch import finish_managed_launch_preflight
from zerg.cli._managed_launch import interactive_human_shell_launch_provenance
from zerg.cli._managed_launch import launch_managed_local_from_api as _launch_managed_local_from_api
from zerg.cli._managed_launch import maybe_open_session_url
from zerg.cli._managed_launch import record_contract_or_warn
from zerg.cli._managed_launch import resolve_managed_launch_credentials as _load_api_credentials
from zerg.cli._managed_launch import start_managed_launch
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PATH
from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_SERVER_NAME
from zerg.services.claude_channel_bridge import build_claude_channel_exec_command
from zerg.services.claude_channel_bridge import remove_claude_channel_mcp_server
from zerg.services.claude_channel_bridge import wait_for_claude_channel_state
from zerg.services.longhouse_paths import get_agent_runtime_events_outbox_dir
from zerg.services.machine_identity import get_machine_name_label
from zerg.services.shipper.hooks import install_hooks
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_loop_mode import SessionLoopMode

# See ARCHITECTURE.md's "Session modes" section: terminal-originated
# `launch_local` is Helm; `turn_start` is Console.


class _NativeClaudeError(Exception):
    """Raised when native Claude launch preparation fails."""


_CLAUDE_LAUNCH_ENV_KEYS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ANTHROPIC_MODEL",
)
_FORCE_NATIVE_CLAUDE_CHANNELS_ENV = "LONGHOUSE_FORCE_NATIVE_CLAUDE_CHANNELS"
_CLAUDE_TERMINAL_POST_TIMEOUT_SECS = 2.0
_CLAUDE_TERMINAL_SOURCE = "claude_channel_wrapper"
_CLAUDE_SUBPROCESS_ENV_BLOCKLIST = (
    "CLAUDE_CONFIG_DIR",
    "LONGHOUSE_COORDINATION_TOKEN",
    "LONGHOUSE_MANAGED_SESSION_ID",
    "LONGHOUSE_SESSION_ID",
    "LONGHOUSE_CHANNEL_SESSION_ID",
    "LONGHOUSE_PROVIDER_SESSION_ID",
    "LONGHOUSE_RUN_ID",
    "LONGHOUSE_HOOK_TOKEN",
)
_CLAUDE_BIN_ENV = "LONGHOUSE_CLAUDE_BIN"
_CLAUDE_CHANNEL_READY_TIMEOUT_SECS = 20.0


def _claude_subprocess_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in _CLAUDE_SUBPROCESS_ENV_BLOCKLIST:
        env.pop(key, None)
    env.update(extra)
    return env


def _resolve_claude_command() -> str:
    explicit = str(os.environ.get(_CLAUDE_BIN_ENV) or "").strip()
    if explicit:
        return explicit
    return shutil.which("claude") or "claude"


def _run_claude_auth_status() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_resolve_claude_command(), "auth", "status", "--json"],
        check=False,
        capture_output=True,
        env=_claude_subprocess_env(),
        text=True,
        timeout=5,
    )


def _detect_native_claude_channels_available() -> tuple[bool, str]:
    try:
        completed = _run_claude_auth_status()
    except subprocess.TimeoutExpired as exc:
        return False, f"claude auth status timed out after {exc.timeout}s"
    except OSError as exc:
        return False, f"claude auth status unavailable: {type(exc).__name__}: {exc}"

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        detail = stderr or f"claude auth status exited {completed.returncode}"
        return False, detail

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return False, "claude auth status returned invalid JSON"

    logged_in = bool(payload.get("loggedIn"))
    auth_method = str(payload.get("authMethod") or "").strip()
    api_provider = str(payload.get("apiProvider") or "").strip()
    if logged_in:
        return True, f"authMethod={auth_method}, apiProvider={api_provider}"
    return False, "Claude is not logged in"


def _collect_claude_launch_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _CLAUDE_LAUNCH_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
    launch_actor, launch_surface = interactive_human_shell_launch_provenance()
    if launch_actor:
        env["LONGHOUSE_LAUNCH_ACTOR"] = launch_actor
    if launch_surface:
        env["LONGHOUSE_LAUNCH_SURFACE"] = launch_surface
    return env


def _launch_env_requires_flag_capable_claude_path(claude_launch_env: dict[str, str]) -> bool:
    return bool(str(claude_launch_env.get("CLAUDE_CODE_USE_BEDROCK") or "").strip())


def _is_truthy_env_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _force_native_claude_channels_enabled() -> bool:
    return _is_truthy_env_value(os.environ.get(_FORCE_NATIVE_CLAUDE_CHANNELS_ENV))


def _result_uses_native_claude_bridge(result: ManagedLocalLaunchResponse) -> bool:
    return result.managed_transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value


def _run_attach_command(attach_command: str) -> int:
    parts = shlex.split(attach_command)
    completed = subprocess.run(parts, check=False)
    return int(completed.returncode)


def _resolve_claude_dir(config_dir: Path | None) -> Path:
    return config_dir or (Path.home() / ".claude")


def _ensure_native_claude_prereqs(
    *,
    base_url: str,
    token: str,
    workspace_path: Path,
    config_dir: Path | None,
) -> None:
    try:
        resolved_claude_dir = _resolve_claude_dir(config_dir)
        install_hooks(base_url, token=token, claude_dir=str(resolved_claude_dir))
        remove_claude_channel_mcp_server(claude_dir=resolved_claude_dir)
    except Exception as exc:  # pragma: no cover - exercised through CLI wrappers
        raise _NativeClaudeError(str(exc)) from exc


def _run_native_claude_tui(
    *,
    session_id: str,
    run_id: str | None,
    provider_session_id: str,
    cwd: Path,
    base_url: str,
    token: str,
    hook_token: str | None = None,
    coordination_token: str | None = None,
    permission_mode: str = "bypass",
    resume: bool = False,
) -> int:
    """ARCHITECTURE.md's "Session modes": Helm launched from a physical terminal.
    Runs in the foreground and blocks until the user exits Claude.
    """
    if not coordination_token:
        raise _NativeClaudeError("Longhouse did not issue coordination authority for this session")
    # In remote-approve mode the permission gate authenticates as this session
    # using the server-minted session-scoped hook token (the gate rejects the
    # durable device token). Other hooks keep using the device token.
    env = _claude_subprocess_env()
    if hook_token:
        env["LONGHOUSE_HOOK_TOKEN"] = hook_token
    threading.Thread(
        target=_warn_if_claude_channel_not_ready,
        kwargs={"session_id": session_id},
        daemon=True,
    ).start()
    with tempfile.TemporaryDirectory(prefix="longhouse-claude-mcp-") as temp_dir:
        mcp_config_path = Path(temp_dir) / "mcp.json"
        mcp_config = {
            "mcpServers": {
                CLAUDE_CHANNEL_SERVER_NAME: {
                    "type": "stdio",
                    "command": "longhouse-engine",
                    "args": ["claude-channel", "serve"],
                    "env": {
                        "LONGHOUSE_COORDINATION_TOKEN": coordination_token,
                        "LONGHOUSE_CHANNEL_SESSION_ID": session_id,
                    },
                }
            }
        }
        fd = os.open(mcp_config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(mcp_config, handle, separators=(",", ":"))
        command = build_claude_channel_exec_command(
            provider_session_id=provider_session_id,
            longhouse_session_id=session_id,
            longhouse_run_id=run_id,
            cwd=str(cwd),
            resume=resume,
            hook_url=base_url,
            claude_command=_resolve_claude_command(),
            permission_mode=permission_mode,
            mcp_config_path=mcp_config_path,
        )
        completed = subprocess.run(shlex.split(command), check=False, cwd=str(cwd), env=env)
    exit_code = int(completed.returncode)
    _post_claude_terminal_signal(
        base_url=base_url,
        token=token,
        session_id=session_id,
        provider_session_id=provider_session_id,
        exit_code=exit_code,
    )
    return exit_code


def _warn_if_claude_channel_not_ready(*, session_id: str) -> None:
    try:
        wait_for_claude_channel_state(
            session_id=session_id,
            timeout_secs=_CLAUDE_CHANNEL_READY_TIMEOUT_SECS,
        )
    except Exception as exc:
        typer.secho(
            "Longhouse control channel did not become ready; this Claude session may be observe-only. "
            f"Keep Claude open and check its Longhouse connection ({exc}).",
            err=True,
            fg=typer.colors.YELLOW,
        )


def _post_claude_terminal_signal(
    *,
    base_url: str,
    token: str,
    session_id: str,
    provider_session_id: str,
    exit_code: int,
) -> bool:
    occurred_at = datetime.now(timezone.utc).isoformat()
    terminal_state = "session_ended" if exit_code == 0 else "process_gone"
    event = _build_claude_terminal_event(
        session_id=session_id,
        provider_session_id=provider_session_id,
        exit_code=exit_code,
        terminal_state=terminal_state,
        occurred_at=occurred_at,
    )
    queued_path = _queue_claude_terminal_runtime_event(event)
    if queued_path is not None:
        return True

    try:
        with httpx.Client(timeout=_CLAUDE_TERMINAL_POST_TIMEOUT_SECS) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/runtime/events/batch",
                headers={"X-Agents-Token": token},
                json={"events": [event]},
            )
            response.raise_for_status()
            return True
    except Exception as exc:
        typer.secho(
            f"Could not confirm Claude terminal lifecycle event after local queue failed ({exc}). "
            "Machine Agent will reconcile if it observes the provider exit.",
            fg=typer.colors.YELLOW,
        )
        return False


def _build_claude_terminal_event(
    *,
    session_id: str,
    provider_session_id: str,
    exit_code: int,
    terminal_state: str,
    occurred_at: str,
) -> dict:
    return {
        "runtime_key": f"claude:{provider_session_id}",
        "session_id": session_id,
        "provider": "claude",
        "device_id": get_machine_name_label(),
        "source": _CLAUDE_TERMINAL_SOURCE,
        "kind": "terminal_signal",
        "occurred_at": occurred_at,
        "dedupe_key": f"claude-terminal:{provider_session_id}:{exit_code}:{occurred_at}",
        "payload": {
            "terminal_state": terminal_state,
            "terminal_reason": "provider_exit",
            "terminal_source": _CLAUDE_TERMINAL_SOURCE,
            "provider_session_id": provider_session_id,
            "exit_code": exit_code,
        },
    }


def _queue_claude_terminal_runtime_event(event: dict) -> Path | None:
    try:
        outbox_dir = get_agent_runtime_events_outbox_dir()
        outbox_dir.mkdir(parents=True, exist_ok=True)
        digest_seed = f"{event.get('source', '')}:{event.get('dedupe_key', '')}"
        file_digest = sha256(digest_seed.encode("utf-8")).hexdigest()[:32]
        final_path = outbox_dir / f"rte.{file_digest}.json"
        tmp_path = outbox_dir / f".tmp.{file_digest}.{os.getpid()}"
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with tmp_path.open("wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, final_path)
        return final_path
    except Exception as exc:
        typer.secho(
            f"Could not queue Claude terminal lifecycle event locally ({exc}).",
            fg=typer.colors.YELLOW,
        )
        return None


def _finalize_native_claude_launch(
    *,
    base_url: str,
    token: str,
    cwd: Path,
    result: ManagedLocalLaunchResponse,
    config_dir: Path | None,
    open_browser: bool,
    attach: bool,
    machine_name: str,
    verbose: bool,
    resume: bool = False,
) -> None:
    session_url = _build_session_url(base_url, result.session_id)
    launch_ui.launch_panel(
        provider_label=launch_ui.PROVIDER_LABELS["claude"],
        base_url=base_url,
        machine_name=machine_name,
        session_id=result.session_id,
        verbose=verbose,
        attach_command=result.attach_command,
    )

    maybe_open_session_url(open_browser=open_browser, session_url=session_url, opener=_open_session_url)

    if not attach:
        return
    if not _interactive_stdio():
        typer.secho("Skipping native launch because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        return
    if not result.provider_session_id:
        typer.secho(
            "Longhouse cannot attach native Claude until the provider reports a real session id.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    launch_ui.progress("Launching Claude…")
    record_contract_or_warn(
        provider="claude",
        session_id=result.session_id,
        cwd=cwd,
        config_dir=config_dir,
        launch_mode="tui",
        provider_binary_path=_resolve_claude_command(),
        provider_binary_source=PROVIDER_CLI_SOURCE_PATH,
        control_kind="claude_channel_bridge",
        config_dir_is_provider_home=True,
    )
    try:
        exit_code = _run_native_claude_tui(
            session_id=result.session_id,
            run_id=result.run_id or None,
            provider_session_id=result.provider_session_id,
            cwd=cwd,
            base_url=base_url,
            token=token,
            hook_token=result.hook_token,
            coordination_token=result.coordination_token,
            permission_mode=result.permission_mode,
            resume=resume,
        )
    finally:
        remove_managed_provider_contract(
            provider="claude",
            session_id=result.session_id,
            config_dir=config_dir,
            config_dir_is_provider_home=True,
        )
    launch_ui.exit_bookend(
        exit_code=exit_code,
        machine_name=machine_name,
        reattach_command=f"longhouse claude --resume {result.session_id}",
    )


def _resolve_native_claude_resume(
    *,
    base_url: str,
    token: str,
    session_id: str,
    machine_name: str,
) -> tuple[ManagedLocalLaunchResponse, Path]:
    try:
        resolved_session_id = str(UUID(session_id))
    except ValueError as exc:
        raise _NativeClaudeError("--resume must be a Longhouse session UUID") from exc

    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/api/agents/sessions/{resolved_session_id}",
                headers={"X-Agents-Token": token},
            )
    except httpx.HTTPError as exc:
        raise _NativeClaudeError(f"Could not load Claude session {resolved_session_id}: {exc}") from exc
    if response.status_code == 401:
        raise _NativeClaudeError("Authentication failed. Run 'longhouse auth' to re-authenticate.")
    if response.status_code == 404:
        raise _NativeClaudeError(f"Session not found: {resolved_session_id}")
    if response.status_code != 200:
        raise _NativeClaudeError(f"Could not load Claude session {resolved_session_id}: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise _NativeClaudeError("Longhouse returned invalid session JSON") from exc
    if not isinstance(payload, dict) or str(payload.get("provider") or "").strip() != "claude":
        raise _NativeClaudeError("--resume requires an existing Claude session")
    provider_session_id = str(response.headers.get("X-Provider-Session-ID") or "").strip()
    if not provider_session_id:
        raise _NativeClaudeError("Claude session has no provider resume identity yet")
    try:
        UUID(provider_session_id)
    except ValueError as exc:
        raise _NativeClaudeError("Claude session has an invalid provider resume identity") from exc
    cwd = Path(str(payload.get("cwd") or "").strip())
    if not cwd.is_absolute() or not cwd.is_dir():
        raise _NativeClaudeError(f"Claude session workspace is unavailable: {cwd}")
    permission_mode = str(payload.get("permission_mode") or "bypass").strip() or "bypass"
    try:
        token_response = httpx.post(
            f"{base_url.rstrip('/')}/api/agents/sessions/{resolved_session_id}/coordination-token",
            headers={"X-Agents-Token": token},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise _NativeClaudeError(f"Could not issue resume coordination authority: {exc}") from exc
    if token_response.status_code != 200:
        raise _NativeClaudeError(f"Could not issue resume coordination authority: {token_response.text[:200]}")
    coordination_token = str(token_response.json().get("coordination_token") or "").strip()
    if not coordination_token:
        raise _NativeClaudeError("Longhouse returned empty resume coordination authority")
    attach_command = build_claude_channel_exec_command(
        provider_session_id=provider_session_id,
        longhouse_session_id=resolved_session_id,
        cwd=str(cwd),
        resume=True,
        hook_url=base_url,
        claude_command=_resolve_claude_command(),
        permission_mode=permission_mode,
    )
    return (
        ManagedLocalLaunchResponse(
            session_id=resolved_session_id,
            provider_session_id=provider_session_id,
            attach_command=attach_command,
            source_runner_name=machine_name,
            managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
            permission_mode=permission_mode,
            coordination_token=coordination_token,
        ),
        cwd,
    )


def claude(
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
    name: str | None = typer.Option(None, "--name", help="Optional display name for the Claude session."),
    resume: str | None = typer.Option(
        None,
        "--resume",
        metavar="SESSION_ID",
        help="Resume an existing Longhouse Claude Helm session using Claude's native provider thread.",
    ),
    remote_approve: bool = typer.Option(
        False,
        "--remote-approve/--no-remote-approve",
        help="Pause on tool permission prompts and answer them from Longhouse (web/iOS) "
        "instead of running with --dangerously-skip-permissions. Default is autonomous bypass.",
    ),
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
        "--claude-dir",
        help="Longhouse config directory (default: ~/.claude).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose/--quiet",
        "-v",
        help="Show full session id, timeline URL, and attach command on launch.",
    ),
) -> None:
    """Launch a Longhouse Claude Code session on this machine via the Longhouse API."""

    launch_ui.quiet_diagnostic_logs(verbose)
    resolved_url, resolved_token, resolved_config_dir = start_managed_launch(
        config_dir=config_dir,
        url=url,
        token=token,
        verbose=verbose,
        exit_code=EXIT_SETUP_FAILED,
        load_credentials=_load_api_credentials,
    )
    machine_name = get_machine_name_label()
    claude_launch_env = _collect_claude_launch_env()
    native_claude_channels_available, native_claude_channels_detail = _detect_native_claude_channels_available()
    force_native_claude_channels = _force_native_claude_channels_enabled()
    force_flag_capable_path = _launch_env_requires_flag_capable_claude_path(claude_launch_env)
    if force_native_claude_channels:
        native_claude_channels_available = True
        native_claude_channels_detail = f"forced by {_FORCE_NATIVE_CLAUDE_CHANNELS_ENV}"
        force_flag_capable_path = False
        if verbose:
            message = f"Forcing native Claude channels via {_FORCE_NATIVE_CLAUDE_CHANNELS_ENV}=1."
            typer.secho(f"{message} This is a private unsupported local experiment.", fg=typer.colors.YELLOW)
    elif force_flag_capable_path:
        native_claude_channels_available = False
        native_claude_channels_detail = "disabled by Claude launch env"
    if not native_claude_channels_available:
        typer.secho(
            f"Native Claude channels unavailable ({native_claude_channels_detail}). "
            "Longhouse now requires the local Claude channel bridge.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    finish_managed_launch_preflight(
        url=resolved_url,
        machine_name=machine_name,
        config_dir=resolved_config_dir,
        exit_code=EXIT_SETUP_FAILED,
        verbose=verbose,
        run_preflight=_ensure_managed_launch_preflight,
    )
    try:
        _ensure_native_claude_prereqs(
            base_url=resolved_url,
            token=resolved_token,
            workspace_path=cwd,
            config_dir=resolved_config_dir,
        )
    except _NativeClaudeError as exc:
        typer.secho(f"Claude bridge setup failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    if resume:
        try:
            resume_result, resume_cwd = _resolve_native_claude_resume(
                base_url=resolved_url,
                token=resolved_token,
                session_id=resume,
                machine_name=machine_name,
            )
        except _NativeClaudeError as exc:
            typer.secho(f"Claude resume failed: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=EXIT_SETUP_FAILED)
        _finalize_native_claude_launch(
            base_url=resolved_url,
            token=resolved_token,
            cwd=resume_cwd,
            result=resume_result,
            config_dir=Path(config_dir) if config_dir else None,
            open_browser=open_browser,
            attach=attach,
            machine_name=machine_name,
            verbose=verbose,
            resume=True,
        )
        return
    result = _launch_managed_local_from_api(
        url=resolved_url,
        token=resolved_token,
        cwd=cwd,
        project=project,
        loop_mode=loop_mode,
        name=name,
        machine_name=machine_name,
        native_claude_channels_available=native_claude_channels_available,
        claude_launch_env=claude_launch_env,
        provider="claude",
        permission_mode="remote_approve" if remote_approve else "bypass",
        verbose=verbose,
    )
    if force_flag_capable_path and _result_uses_native_claude_bridge(result):
        typer.secho(
            "Longhouse returned the native Claude bridge for a launch that requires the permissive Claude path.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    resolved_claude_dir = Path(config_dir) if config_dir else None
    if not _result_uses_native_claude_bridge(result):
        typer.secho(
            "Longhouse returned an unsupported managed-local transport for Claude.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    _finalize_native_claude_launch(
        base_url=resolved_url,
        token=resolved_token,
        cwd=cwd,
        result=result,
        config_dir=resolved_claude_dir,
        open_browser=open_browser,
        attach=attach,
        machine_name=machine_name,
        verbose=verbose,
    )
