"""Longhouse Antigravity CLI session launcher."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError
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
from zerg.cli._managed_contract import record_managed_provider_contract
from zerg.provider_cli_contract import ANTIGRAVITY_BIN_ENV
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_ANTIGRAVITY_BIN_FLAG
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PATH
from zerg.services.longhouse_paths import get_managed_local_dir
from zerg.services.longhouse_paths import resolve_longhouse_home
from zerg.services.session_continuity import get_machine_name_label
from zerg.session_loop_mode import SessionLoopMode

_ANTIGRAVITY_RUNTIME_SOURCE = "antigravity_event"
_ANTIGRAVITY_RUNTIME_EVENT_TIMEOUT_SECONDS = 5
_ANTIGRAVITY_PLUGIN_NAME = "longhouse-runtime"
_ANTIGRAVITY_HOOK_SCRIPT_NAME = "longhouse-antigravity-hook.sh"
_ANTIGRAVITY_BIN_OPTION_HELP = " ".join(
    [
        "Debug override for the Antigravity CLI executable used by managed sessions",
        f"(defaults to {ANTIGRAVITY_BIN_ENV}, then `agy` on PATH).",
    ]
)


class _AntigravityLaunchError(Exception):
    """Raised when native Antigravity launch preparation fails."""


_ANTIGRAVITY_HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse Antigravity hook - local presence outbox + managed transcript binding.
INPUT=$(/bin/cat)
EVENT="${1:-}"
LONGHOUSE_HOME="${LONGHOUSE_HOME:-}"
if [ -z "$LONGHOUSE_HOME" ]; then
  LONGHOUSE_HOME=__LONGHOUSE_HOME__
fi
ENGINE="${LONGHOUSE_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  ENGINE=__ENGINE_PATH__
fi
PYTHON="${LONGHOUSE_HOOK_PYTHON:-python3}"

emit_default_response() {
  case "$EVENT" in
    PreInvocation) printf '{"injectSteps":[]}\\n' ;;
    PostInvocation) printf '{"injectSteps":[],"terminationBehavior":""}\\n' ;;
    Stop) printf '{"decision":"","reason":""}\\n' ;;
    *) printf '{}\\n' ;;
  esac
}

if ! "$PYTHON" -c 'import json' >/dev/null 2>&1; then
  emit_default_response
  exit 0
fi

LONGHOUSE_HOOK_INPUT="$INPUT" \\
LONGHOUSE_HOOK_EVENT="$EVENT" \\
LONGHOUSE_HOOK_HOME="$LONGHOUSE_HOME" \\
LONGHOUSE_HOOK_ENGINE="$ENGINE" \\
"$PYTHON" - <<'PY' || emit_default_response
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def default_response(event: str) -> dict:
    if event == "PreInvocation":
        return {"injectSteps": []}
    if event == "PreToolUse":
        return {"decision": "allow", "reason": ""}
    if event == "PostInvocation":
        return {"injectSteps": [], "terminationBehavior": ""}
    if event == "Stop":
        return {"decision": "allow", "reason": ""}
    return {}


def write_presence_outbox(longhouse_home: str, payload: dict) -> None:
    outbox = Path(longhouse_home) / "agent" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp.", dir=str(outbox))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.write("\\n")
        final_name = tmp_path.name.replace(".tmp.", "prs.", 1)
        tmp_path.replace(outbox / f"{final_name}.json")
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass


event = os.environ.get("LONGHOUSE_HOOK_EVENT", "")
try:
    data = json.loads(os.environ.get("LONGHOUSE_HOOK_INPUT") or "{}")
except Exception:
    data = {}

conversation_id = str(data.get("conversationId") or "")
tool_call = data.get("toolCall") or data.get("tool_call") or {}
tool = str(tool_call.get("name") or "") if isinstance(tool_call, dict) else ""
workspace_paths = data.get("workspacePaths") or []
cwd = str(workspace_paths[0] or "") if isinstance(workspace_paths, list) and workspace_paths else ""
transcript = str(data.get("transcriptPath") or "")
step_index = str(data.get("stepIdx") or data.get("step_index") or "")
managed_session_id = os.environ.get("LONGHOUSE_MANAGED_SESSION_ID") or ""
session_id = managed_session_id or conversation_id

state = ""
if event == "PreInvocation":
    state = "thinking"
elif event == "PreToolUse":
    state = "running"
elif event in {"PostToolUse", "PostInvocation"}:
    state = "thinking"
elif event == "Stop":
    fully_idle = data.get("fullyIdle")
    state = "idle" if fully_idle is True or str(fully_idle).lower() in {"1", "true"} else "running"

if state and session_id:
    longhouse_home = os.environ.get("LONGHOUSE_HOOK_HOME", "")
    write_presence_outbox(
        longhouse_home,
        {
            "session_id": session_id,
            "state": state,
            "tool_name": tool,
            "cwd": cwd,
            "provider": "antigravity",
            "transcript_path": transcript,
            "step_index": step_index,
        },
    )
    if managed_session_id and transcript:
        try:
            bind_env = os.environ.copy()
            if longhouse_home:
                bind_env["LONGHOUSE_HOME"] = longhouse_home
            bind_args = [
                os.environ.get("LONGHOUSE_HOOK_ENGINE") or "longhouse-engine",
                "bind",
                "--path",
                transcript,
                "--session-id",
                managed_session_id,
                "--provider",
                "antigravity",
            ]
            if longhouse_home:
                bind_args.extend(["--db", str(Path(longhouse_home) / "agent" / "longhouse-shipper.db")])
            subprocess.run(
                bind_args,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=bind_env,
            )
        except Exception:
            pass

print(json.dumps(default_response(event), separators=(",", ":")))
PY
exit 0
"""


def _resolve_explicit_antigravity_binary(candidate: str, *, source: str) -> str:
    normalized = str(candidate or "").strip()
    if not normalized:
        raise _AntigravityLaunchError(f"{source} is empty")
    looks_like_path = normalized.startswith((".", "~", "/")) or "/" in normalized or "\\" in normalized
    if looks_like_path:
        path = Path(os.path.expanduser(normalized))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise _AntigravityLaunchError(f"{source} points to `{candidate}`, but it is not an executable file.")
    resolved = shutil.which(normalized)
    if resolved:
        return resolved
    raise _AntigravityLaunchError(f"{source} points to `{candidate}`, but it was not found on PATH.")


def _resolve_antigravity_binary(explicit: str | None = None) -> str | None:
    normalized = str(explicit or "").strip()
    if normalized:
        return _resolve_explicit_antigravity_binary(
            normalized,
            source=PROVIDER_CLI_SOURCE_ANTIGRAVITY_BIN_FLAG,
        )
    env_candidate = str(os.environ.get(ANTIGRAVITY_BIN_ENV) or "").strip()
    if env_candidate:
        return _resolve_explicit_antigravity_binary(env_candidate, source=ANTIGRAVITY_BIN_ENV)
    return shutil.which("agy")


def _antigravity_binary_source(explicit: str | None, resolved: str | None) -> str:
    if str(explicit or "").strip():
        return PROVIDER_CLI_SOURCE_ANTIGRAVITY_BIN_FLAG
    if str(os.environ.get(ANTIGRAVITY_BIN_ENV) or "").strip():
        return ANTIGRAVITY_BIN_ENV
    return PROVIDER_CLI_SOURCE_PATH if resolved else "missing"


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
        provider="antigravity",
    )


def _managed_runtime_events_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/agents/runtime/events/batch"


def _antigravity_staged_plugin_root(antigravity_cli_root: Path | None = None) -> Path:
    cli_root = antigravity_cli_root or (Path.home() / ".gemini" / "antigravity-cli")
    return cli_root / "plugins" / _ANTIGRAVITY_PLUGIN_NAME


def _antigravity_runtime_dir(config_dir: Path | None = None) -> Path:
    return get_managed_local_dir("antigravity", base_dir=config_dir)


def _antigravity_plugin_source_root(config_dir: Path | None = None) -> Path:
    return _antigravity_runtime_dir(config_dir) / "plugins" / _ANTIGRAVITY_PLUGIN_NAME


def _antigravity_global_hooks_path() -> Path:
    return Path.home() / ".gemini" / "config" / "hooks.json"


def _default_engine_path() -> str:
    try:
        from zerg.services.shipper.service import get_engine_executable

        return get_engine_executable()
    except RuntimeError:
        return "longhouse-engine"


def _write_text_if_changed(path: Path, content: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                if mode is not None:
                    path.chmod(mode)
                return
        except OSError:
            pass
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _antigravity_hook_config(command_prefix: str) -> dict:
    return {
        "PreInvocation": [
            {"type": "command", "command": f"{command_prefix} PreInvocation", "timeout": 5},
        ],
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": f"{command_prefix} PreToolUse", "timeout": 5}],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": f"{command_prefix} PostToolUse", "timeout": 5}],
            },
        ],
        "PostInvocation": [
            {"type": "command", "command": f"{command_prefix} PostInvocation", "timeout": 5},
        ],
        "Stop": [
            {"type": "command", "command": f"{command_prefix} Stop", "timeout": 5},
        ],
    }


def _upsert_antigravity_global_hooks(*, hooks_path: Path, hook_config: dict) -> None:
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict
    if hooks_path.exists() and hooks_path.stat().st_size > 0:
        try:
            loaded = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise _AntigravityLaunchError(f"Could not parse existing Antigravity hooks file `{hooks_path}`.") from exc
        if not isinstance(loaded, dict):
            raise _AntigravityLaunchError(f"Existing Antigravity hooks file `{hooks_path}` must contain a JSON object.")
        existing = loaded
    else:
        existing = {}
    existing[_ANTIGRAVITY_PLUGIN_NAME] = hook_config
    _write_text_if_changed(hooks_path, json.dumps(existing, indent=2) + "\n")


def _ensure_antigravity_runtime_plugin(
    *,
    config_dir: Path | None = None,
    antigravity_cli_root: Path | None = None,
    engine_path: str | None = None,
    antigravity_bin: str | None = None,
    global_hooks_path: Path | None = None,
) -> Path:
    staged_root = _antigravity_staged_plugin_root(antigravity_cli_root)
    plugin_root = _antigravity_plugin_source_root(config_dir) if antigravity_bin else staged_root
    hook_script = plugin_root / _ANTIGRAVITY_HOOK_SCRIPT_NAME
    longhouse_home = resolve_longhouse_home(config_dir)
    hook_content = _ANTIGRAVITY_HOOK_SCRIPT.replace(
        "__LONGHOUSE_HOME__",
        shlex.quote(str(longhouse_home)),
    ).replace(
        "__ENGINE_PATH__",
        shlex.quote(engine_path or _default_engine_path()),
    )
    _write_text_if_changed(
        plugin_root / "plugin.json",
        json.dumps({"name": _ANTIGRAVITY_PLUGIN_NAME}, indent=2) + "\n",
    )
    _write_text_if_changed(hook_script, hook_content, mode=0o755)
    command_prefix = shlex.quote(str(hook_script))
    hook_config = _antigravity_hook_config(command_prefix)
    hooks = {_ANTIGRAVITY_PLUGIN_NAME: hook_config}
    _write_text_if_changed(plugin_root / "hooks.json", json.dumps(hooks, indent=2) + "\n")
    _upsert_antigravity_global_hooks(
        hooks_path=global_hooks_path or _antigravity_global_hooks_path(),
        hook_config=hook_config,
    )
    if antigravity_bin:
        completed = subprocess.run(
            [antigravity_bin, "plugin", "install", str(plugin_root)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            raise _AntigravityLaunchError("Could not install Longhouse Antigravity plugin" + (f": {detail}" if detail else "."))
    return staged_root


def _write_antigravity_launch_script(
    *,
    config_dir: Path | None,
    session_id: str,
    device_id: str,
    antigravity_bin: str,
    cwd: Path,
    runtime_events_url: str,
    token: str,
) -> Path:
    _ensure_antigravity_runtime_plugin(config_dir=config_dir, antigravity_bin=antigravity_bin)
    runtime_dir = _antigravity_runtime_dir(config_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    script_path = runtime_dir / f"{session_id}.launch.sh"
    script = f"""#!/bin/sh
export LONGHOUSE_MANAGED_SESSION_ID={shlex.quote(session_id)}
export LONGHOUSE_DEVICE_ID={shlex.quote(device_id)}
export LONGHOUSE_RUNTIME_EVENTS_URL={shlex.quote(runtime_events_url)}
export LONGHOUSE_RUNTIME_TOKEN={shlex.quote(token)}
export LONGHOUSE_HOOK_PYTHON={shlex.quote(sys.executable)}
cd {shlex.quote(str(cwd))} || exit 1
{shlex.quote(antigravity_bin)} "$@"
status=$?
LONGHOUSE_RUNTIME_STATUS="$status" \\
LONGHOUSE_RUNTIME_SESSION_ID={shlex.quote(session_id)} \\
LONGHOUSE_RUNTIME_DEVICE_ID={shlex.quote(device_id)} \\
/usr/bin/env python3 - <<'PY' >/dev/null 2>&1 || true
from datetime import datetime, timezone
import json
import os
import urllib.request

status = int(os.environ.get("LONGHOUSE_RUNTIME_STATUS") or "1")
session_id = os.environ["LONGHOUSE_RUNTIME_SESSION_ID"]
event = {{
    "runtime_key": f"antigravity:{{session_id}}",
    "session_id": session_id,
    "provider": "antigravity",
    "device_id": os.environ["LONGHOUSE_RUNTIME_DEVICE_ID"],
    "source": "antigravity_event",
    "kind": "terminal_signal",
    "phase": "finished",
    "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "dedupe_key": f"{{session_id}}:antigravity_event:terminal:{{status}}",
    "payload": {{"terminal_state": "session_ended", "exit_code": status}},
}}
request = urllib.request.Request(
    os.environ["LONGHOUSE_RUNTIME_EVENTS_URL"],
    data=json.dumps({{"events": [event]}}).encode("utf-8"),
    method="POST",
    headers={{
        "Content-Type": "application/json",
        "X-Agents-Token": os.environ["LONGHOUSE_RUNTIME_TOKEN"],
    }},
)
urllib.request.urlopen(request, timeout=5).read()
PY
exit "$status"
"""
    fd = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(script)
    return script_path


def _runtime_event_payload(
    *,
    session_id: str,
    device_id: str,
    kind: str,
    phase: str | None,
    dedupe_key: str,
    payload: dict,
) -> dict:
    return {
        "runtime_key": f"antigravity:{session_id}",
        "session_id": session_id,
        "provider": "antigravity",
        "device_id": device_id,
        "source": _ANTIGRAVITY_RUNTIME_SOURCE,
        "kind": kind,
        "phase": phase,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dedupe_key": dedupe_key,
        "payload": payload,
    }


def _post_antigravity_runtime_event(
    *,
    url: str,
    token: str,
    event: dict,
) -> None:
    data = json.dumps({"events": [event]}).encode("utf-8")
    request = Request(
        _managed_runtime_events_url(url),
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agents-Token": token,
        },
    )
    try:
        with urlopen(request, timeout=_ANTIGRAVITY_RUNTIME_EVENT_TIMEOUT_SECONDS) as response:
            response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise _AntigravityLaunchError(f"Could not send Antigravity runtime event to Longhouse: {exc}") from exc


def _build_antigravity_command(
    *,
    session_id: str,
    machine_name: str,
    antigravity_bin: str,
    cwd: Path,
    antigravity_args: tuple[str, ...],
    launch_script_path: Path | None = None,
) -> str:
    if launch_script_path is not None:
        command = " ".join([shlex.quote(str(launch_script_path)), *(shlex.quote(arg) for arg in antigravity_args)])
        return f"cd {shlex.quote(str(cwd))} && {command}"

    env_items = [
        f"LONGHOUSE_MANAGED_SESSION_ID={shlex.quote(session_id)}",
        f"LONGHOUSE_DEVICE_ID={shlex.quote(machine_name)}",
    ]
    env_prefix = " ".join(env_items)
    command = " ".join([shlex.quote(antigravity_bin), *(shlex.quote(arg) for arg in antigravity_args)])
    return f"cd {shlex.quote(str(cwd))} && {env_prefix} {command}"


def _run_native_antigravity(
    *,
    session_id: str,
    machine_name: str,
    antigravity_bin: str,
    cwd: Path,
    antigravity_args: tuple[str, ...],
    url: str,
    token: str,
    config_dir: Path | None = None,
) -> int:
    _ensure_antigravity_runtime_plugin(config_dir=config_dir, antigravity_bin=antigravity_bin)
    cmd = [antigravity_bin, *antigravity_args]
    env = os.environ.copy()
    env["LONGHOUSE_MANAGED_SESSION_ID"] = session_id
    env["LONGHOUSE_DEVICE_ID"] = machine_name
    env["LONGHOUSE_RUNTIME_EVENTS_URL"] = _managed_runtime_events_url(url)
    env["LONGHOUSE_RUNTIME_TOKEN"] = token
    env["LONGHOUSE_HOOK_PYTHON"] = sys.executable
    launched = False
    returncode = 1
    try:
        completed = subprocess.run(cmd, check=False, cwd=str(cwd), env=env)
        launched = True
        returncode = int(completed.returncode)
        return returncode
    finally:
        if launched:
            terminal_state = "session_ended"
        else:
            terminal_state = "launch_failed"
        try:
            _post_antigravity_runtime_event(
                url=url,
                token=token,
                event=_runtime_event_payload(
                    session_id=session_id,
                    device_id=machine_name,
                    kind="terminal_signal",
                    phase="finished",
                    dedupe_key=f"{session_id}:{_ANTIGRAVITY_RUNTIME_SOURCE}:terminal:{terminal_state}:{returncode}",
                    payload={"terminal_state": terminal_state, "exit_code": returncode},
                ),
            )
        except _AntigravityLaunchError as exc:
            typer.secho(f"Longhouse runtime event warning: {exc}", fg=typer.colors.YELLOW, err=True)


def antigravity(
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
    name: str | None = typer.Option(None, "--name", help="Optional display name for the agy session."),
    attach: bool = typer.Option(
        True,
        "--attach/--no-attach",
        help="Launch agy after creating the Longhouse session when running interactively.",
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
        help="Longhouse home directory override (default: ~/.longhouse).",
    ),
    antigravity_bin: str | None = typer.Option(
        None,
        "--agy-bin",
        "--antigravity-bin",
        help=_ANTIGRAVITY_BIN_OPTION_HELP,
    ),
) -> None:
    """Launch a Longhouse-managed agy session on this machine.

    Extra arguments after the Longhouse options are passed to the stock
    `agy` executable.
    """

    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = _load_api_credentials(
        url=url,
        token=token,
        config_dir=resolved_config_dir,
        exit_code=managed_local_cli.EXIT_SETUP_FAILED,
    )
    try:
        resolved_antigravity_bin = _resolve_antigravity_binary(antigravity_bin)
    except _AntigravityLaunchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if not resolved_antigravity_bin:
        typer.secho(
            "Antigravity CLI executable not found. Install Antigravity CLI so `agy` is on PATH, "
            f"or set {ANTIGRAVITY_BIN_ENV} / --agy-bin explicitly for debugging.",
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
    typer.secho("Longhouse agy session launched on this machine.", fg=typer.colors.GREEN)
    typer.echo(f"Session ID: {result.session_id}")
    typer.echo(f"Session URL: {session_url}")

    if open_browser:
        typer.echo("Opening session in browser...")
        if not _open_session_url(session_url):
            typer.secho(f"Could not open browser automatically. Visit: {session_url}", fg=typer.colors.YELLOW)

    antigravity_args = tuple(str(arg) for arg in (ctx.args or ()))
    is_interactive = _interactive_stdio()
    command_launch_script_path: Path | None = None
    if not attach or not is_interactive:
        try:
            command_launch_script_path = _write_antigravity_launch_script(
                config_dir=resolved_config_dir,
                session_id=result.session_id,
                device_id=machine_name,
                antigravity_bin=resolved_antigravity_bin,
                cwd=cwd,
                runtime_events_url=_managed_runtime_events_url(resolved_url),
                token=resolved_token,
            )
        except _AntigravityLaunchError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
    command = _build_antigravity_command(
        session_id=result.session_id,
        machine_name=machine_name,
        antigravity_bin=resolved_antigravity_bin,
        cwd=cwd,
        antigravity_args=antigravity_args,
        launch_script_path=command_launch_script_path,
    )
    try:
        record_managed_provider_contract(
            provider="antigravity",
            session_id=result.session_id,
            cwd=cwd,
            config_dir=resolved_config_dir,
            launch_mode="tui" if attach and is_interactive else "launch_script",
            provider_binary_path=resolved_antigravity_bin,
            provider_binary_source=_antigravity_binary_source(antigravity_bin, resolved_antigravity_bin),
            control_kind="antigravity_process",
        )
    except Exception as exc:
        typer.secho(
            f"Longhouse warning: could not record managed-session contract: {exc}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not attach:
        typer.echo(f"Run: {command}")
        return
    if not is_interactive:
        typer.secho("Skipping Antigravity launch because stdin/stdout are not TTYs.", fg=typer.colors.YELLOW)
        typer.echo(f"Run: {command}")
        return

    typer.echo("Launching Antigravity...")
    exit_code = _run_native_antigravity(
        session_id=result.session_id,
        machine_name=machine_name,
        antigravity_bin=resolved_antigravity_bin,
        cwd=cwd,
        antigravity_args=antigravity_args,
        url=resolved_url,
        token=resolved_token,
        config_dir=resolved_config_dir,
    )
    if exit_code != 0:
        typer.secho(f"Antigravity exited with code {exit_code}.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=exit_code)
