"""Longhouse Claude session launcher CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime
from datetime import timezone
from hashlib import sha256
from pathlib import Path

import httpx
import typer

from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli._common import build_session_url as _build_session_url
from zerg.cli._common import ensure_managed_launch_preflight as _ensure_managed_launch_preflight
from zerg.cli._common import git_output
from zerg.cli._common import interactive_stdio as _interactive_stdio
from zerg.cli._common import load_api_credentials
from zerg.cli._common import open_session_url as _open_session_url
from zerg.cli._managed_contract import record_managed_provider_contract
from zerg.cli._managed_contract import remove_managed_provider_contract
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PATH
from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_SERVER_NAME
from zerg.services.claude_channel_bridge import build_claude_channel_exec_command
from zerg.services.claude_channel_bridge import install_claude_channel_mcp_server
from zerg.services.claude_channel_bridge import wait_for_claude_channel_state
from zerg.services.longhouse_paths import get_agent_runtime_events_outbox_dir
from zerg.services.session_continuity import get_machine_name_label
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.services.shipper.hooks import install_hooks
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_loop_mode import SessionLoopMode


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
EXIT_SETUP_FAILED = 78
_CLAUDE_TERMINAL_POST_TIMEOUT_SECS = 2.0
_CLAUDE_TERMINAL_SOURCE = "claude_channel_wrapper"
_CLAUDE_REMOTE_LAUNCH_LOG_DIR = "claude-channel-launch"


def _run_claude_auth_status() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["claude", "auth", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _detect_native_claude_channels_available() -> tuple[bool, str]:
    try:
        completed = _run_claude_auth_status()
    except (OSError, subprocess.TimeoutExpired):
        return False, "claude auth status unavailable"

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
    available = logged_in and auth_method == "claude.ai" and api_provider == "firstParty"
    if available:
        return True, f"authMethod={auth_method}, apiProvider={api_provider}"
    if not logged_in:
        return False, "Claude is not logged in with Claude.ai auth"
    return False, f"authMethod={auth_method or 'unknown'}, apiProvider={api_provider or 'unknown'}"


def _collect_claude_launch_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _CLAUDE_LAUNCH_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
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


def _verify_claude_channel_mcp_server(*, workspace_path: Path, timeout_secs: float = 15.0) -> None:
    try:
        completed = subprocess.run(
            ["claude", "mcp", "get", CLAUDE_CHANNEL_SERVER_NAME],
            cwd=str(workspace_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _NativeClaudeError(f"Could not verify Claude MCP server {CLAUDE_CHANNEL_SERVER_NAME}: {exc}") from exc

    if completed.returncode == 0:
        return

    detail = (completed.stderr or completed.stdout or "").strip()
    if not detail:
        detail = f"claude mcp get {CLAUDE_CHANNEL_SERVER_NAME} exited {completed.returncode}"
    raise _NativeClaudeError(f"Claude cannot resolve MCP server {CLAUDE_CHANNEL_SERVER_NAME}: {detail}")


def _load_api_credentials(
    *,
    url: str | None,
    token: str | None,
    config_dir: Path | None,
    exit_code: int = EXIT_SETUP_FAILED,
) -> tuple[str, str]:
    return load_api_credentials(
        url=url,
        token=token,
        config_dir=config_dir,
        exit_code=exit_code,
        config_dir_is_provider_home=True,
        resolve_url=get_zerg_url,
        resolve_token=load_token,
    )


def _infer_git_context(cwd: Path) -> tuple[str | None, str | None]:
    git_repo = git_output(cwd, "rev-parse", "--show-toplevel")
    git_branch = git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if git_branch == "HEAD":
        git_branch = None
    return git_repo, git_branch


def build_managed_local_launch_payload(
    *,
    cwd: Path,
    provider: str,
    project: str | None,
    name: str | None,
    loop_mode: SessionLoopMode,
    machine_name: str,
    native_claude_channels_available: bool | None = None,
    claude_launch_env: dict[str, str] | None = None,
) -> dict:
    """Build the exact JSON body posted to /api/sessions/managed-local/this-device.

    Public so contract tests can import it and validate against the live
    server schema without reproducing the payload shape in two places.
    """
    git_repo, git_branch = _infer_git_context(cwd)
    payload: dict = {
        "cwd": str(cwd),
        "provider": provider,
        "project": project,
        "git_repo": git_repo,
        "git_branch": git_branch,
        "display_name": name,
        "loop_mode": loop_mode.value,
        "machine_name": machine_name,
    }
    if provider == "claude":
        payload["native_claude_channels_available"] = native_claude_channels_available
        if claude_launch_env:
            payload["claude_launch_env"] = claude_launch_env
    return payload


def _launch_managed_local_from_api(
    *,
    url: str,
    token: str,
    cwd: Path,
    project: str | None,
    loop_mode: SessionLoopMode,
    name: str | None,
    machine_name: str,
    native_claude_channels_available: bool | None = None,
    claude_launch_env: dict[str, str] | None = None,
    provider: str = "claude",
) -> ManagedLocalLaunchResponse:
    payload = build_managed_local_launch_payload(
        cwd=cwd,
        provider=provider,
        project=project,
        name=name,
        loop_mode=loop_mode,
        machine_name=machine_name,
        native_claude_channels_available=native_claude_channels_available,
        claude_launch_env=claude_launch_env,
    )

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{url.rstrip('/')}/api/sessions/managed-local/this-device",
                headers={"X-Agents-Token": token},
                json=payload,
            )
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)
    except httpx.TimeoutException:
        typer.secho(f"Request timed out connecting to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    if response.status_code == 401:
        typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    if response.status_code == 422:
        # Almost always means CLI enum/schema drifted from the server since
        # the user's CLI was installed. Surface a recovery path instead of a
        # raw validation dump.
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
            payload = response.json()
            detail = str(payload.get("detail") or "").strip()
        except ValueError:
            detail = response.text.strip()
        message = detail or response.text[:200] or "Longhouse session launch failed"
        typer.secho(message, fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SETUP_FAILED)

    body = response.json()
    return ManagedLocalLaunchResponse(
        session_id=str(body["session_id"]),
        provider_session_id=str(body["provider_session_id"]),
        attach_command=str(body["attach_command"]),
        source_runner_name=str(body.get("source_runner_name") or machine_name),
        managed_transport=str(body.get("managed_transport") or "") or None,
    )


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
        install_claude_channel_mcp_server(
            workspace_path=workspace_path,
            claude_dir=resolved_claude_dir,
        )
        _verify_claude_channel_mcp_server(workspace_path=workspace_path)
    except Exception as exc:  # pragma: no cover - exercised through CLI wrappers
        raise _NativeClaudeError(str(exc)) from exc


def _run_native_claude_tui(
    *,
    session_id: str,
    provider_session_id: str,
    cwd: Path,
    base_url: str,
    token: str,
) -> int:
    command = build_claude_channel_exec_command(
        provider_session_id=provider_session_id,
        longhouse_session_id=session_id,
        cwd=str(cwd),
        resume=False,
        hook_url=base_url,
        hook_token=token,
    )
    completed = subprocess.run(shlex.split(command), check=False, cwd=str(cwd))
    exit_code = int(completed.returncode)
    _post_claude_terminal_signal(
        base_url=base_url,
        token=token,
        session_id=session_id,
        provider_session_id=provider_session_id,
        exit_code=exit_code,
    )
    return exit_code


def _remote_launch_log_path(*, session_id: str, config_dir: Path | None) -> Path:
    base = _resolve_claude_dir(config_dir) / "logs" / _CLAUDE_REMOTE_LAUNCH_LOG_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{session_id}.log"


def _terminate_detached_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            return


def _launch_detached_native_claude_channel(
    *,
    session_id: str,
    provider_session_id: str,
    cwd: Path,
    base_url: str,
    token: str,
    config_dir: Path | None = None,
    wait_ready_secs: float = 20.0,
) -> dict:
    _ensure_native_claude_prereqs(
        base_url=base_url,
        token=token,
        workspace_path=cwd,
        config_dir=config_dir,
    )
    command = build_claude_channel_exec_command(
        provider_session_id=provider_session_id,
        longhouse_session_id=session_id,
        cwd=str(cwd),
        resume=False,
        hook_url=base_url,
        hook_token=token,
    )
    log_path = _remote_launch_log_path(session_id=session_id, config_dir=config_dir)
    log_file = log_path.open("ab")
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            shlex.split(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        state = wait_for_claude_channel_state(
            session_id=session_id,
            timeout_secs=wait_ready_secs,
        )
    except Exception:
        if process is not None:
            _terminate_detached_process(process)
        raise
    finally:
        log_file.close()

    return {
        "session_id": session_id,
        "provider_session_id": provider_session_id,
        "provider": "claude",
        "transport": ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
        "pid": process.pid if process is not None else None,
        "channel_state": state,
        "log_path": str(log_path),
    }


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
    try:
        with httpx.Client(timeout=_CLAUDE_TERMINAL_POST_TIMEOUT_SECS) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/agents/runtime/events/batch",
                headers={"X-Agents-Token": token},
                json={"events": [event]},
            )
            response.raise_for_status()
            if queued_path is not None:
                queued_path.unlink(missing_ok=True)
            return True
    except Exception as exc:
        if queued_path is not None:
            typer.secho(
                f"Could not confirm Claude terminal lifecycle event before timeout ({exc}). " "Queued for Machine Agent retry.",
                fg=typer.colors.YELLOW,
            )
            return False
        typer.secho(
            f"Could not confirm Claude terminal lifecycle event before timeout ({exc}). "
            "Machine Agent will reconcile if the event was not accepted.",
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
        file_digest = sha256(f"{event.get('source', '')}:{event.get('dedupe_key', '')}".encode("utf-8")).hexdigest()[:32]
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
) -> None:
    session_url = _build_session_url(base_url, result.session_id)
    typer.secho("Longhouse Claude session launched on this machine.", fg=typer.colors.GREEN)
    typer.echo(f"Session ID: {result.session_id}")
    typer.echo(f"Provider session ID: {result.provider_session_id}")
    typer.echo(f"Session URL: {session_url}")
    typer.echo(f"Attach: {result.attach_command}")

    if open_browser:
        typer.echo("Opening session in browser...")
        if not _open_session_url(session_url):
            typer.secho(f"Could not open browser automatically. Visit: {session_url}", fg=typer.colors.YELLOW)

    if not attach:
        return
    if not _interactive_stdio():
        typer.secho("Skipping native launch because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        return

    typer.echo("Launching native Claude...")
    try:
        record_managed_provider_contract(
            provider="claude",
            session_id=result.session_id,
            cwd=cwd,
            config_dir=config_dir,
            launch_mode="tui",
            provider_binary_path=shutil.which("claude"),
            provider_binary_source=PROVIDER_CLI_SOURCE_PATH,
            control_kind="claude_channel_bridge",
            config_dir_is_provider_home=True,
        )
    except Exception as exc:
        typer.secho(
            f"Longhouse warning: could not record managed-session contract: {exc}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    try:
        exit_code = _run_native_claude_tui(
            session_id=result.session_id,
            provider_session_id=result.provider_session_id,
            cwd=cwd,
            base_url=base_url,
            token=token,
        )
    finally:
        remove_managed_provider_contract(
            provider="claude",
            session_id=result.session_id,
            config_dir=config_dir,
            config_dir_is_provider_home=True,
        )
    if exit_code != 0:
        typer.secho(
            f"Native Claude exited with code {exit_code}. Run the printed attach command manually.",
            fg=typer.colors.YELLOW,
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
) -> None:
    """Launch a Longhouse Claude Code session on this machine via the Longhouse API."""

    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = _load_api_credentials(
        url=url,
        token=token,
        config_dir=resolved_config_dir,
        exit_code=EXIT_SETUP_FAILED,
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
        typer.secho(
            f"Forcing native Claude channels via {_FORCE_NATIVE_CLAUDE_CHANNELS_ENV}=1. " "This is a private unsupported local experiment.",
            fg=typer.colors.YELLOW,
        )
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
    _ensure_managed_launch_preflight(
        url=resolved_url,
        machine_name=machine_name,
        config_dir=resolved_config_dir,
        exit_code=EXIT_SETUP_FAILED,
    )
    typer.echo("Preparing native Claude bridge...")
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
    typer.echo(f"Longhouse: {resolved_url}")
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
    )
