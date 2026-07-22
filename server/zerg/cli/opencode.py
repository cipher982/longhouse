"""Longhouse OpenCode session launcher CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

import typer

from zerg.cli import _launch_ui as launch_ui
from zerg.cli import claude as managed_local_cli
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli._common import build_session_url as _build_session_url
from zerg.cli._common import ensure_managed_launch_preflight as _ensure_managed_launch_preflight
from zerg.cli._common import interactive_stdio as _interactive_stdio
from zerg.cli._common import load_api_credentials as _load_api_credentials
from zerg.cli._common import open_session_url as _open_session_url
from zerg.cli._managed_contract import record_managed_provider_contract
from zerg.cli._managed_contract import remove_managed_provider_contract
from zerg.cli._managed_launch import add_interactive_human_shell_launch_env
from zerg.mcp_server.server import COORDINATION_INSTRUCTIONS
from zerg.provider_cli_contract import OPENCODE_BIN_ENV
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_MISSING
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_OPENCODE_BIN_FLAG
from zerg.provider_cli_contract import PROVIDER_CLI_SOURCE_PATH
from zerg.services.longhouse_paths import get_managed_local_dir
from zerg.services.machine_identity import get_machine_name_label
from zerg.services.managed_provider_capability_decisions import evaluate_managed_provider_capability
from zerg.services.opencode_bridge_state import generate_server_password
from zerg.services.opencode_bridge_state import parse_listen_line
from zerg.services.opencode_bridge_state import remove_opencode_bridge_state
from zerg.services.opencode_bridge_state import write_opencode_bridge_state
from zerg.services.provider_capability_contract import ProductAction
from zerg.services.provider_capability_contract import RuntimeState
from zerg.services.provider_capability_evaluator import EvaluationContext
from zerg.session_loop_mode import SessionLoopMode

_OPENCODE_RUNTIME_SOURCE = "opencode_event"
_OPENCODE_RUNTIME_PLUGIN_FILENAME = "longhouse-opencode-runtime.mjs"
_OPENCODE_COORDINATION_INSTRUCTIONS_FILENAME = "longhouse-coordination.md"
_OPENCODE_RUNTIME_EVENT_TIMEOUT_SECONDS = 5
_OPENCODE_RUNTIME_PLUGIN_POST_TIMEOUT_MS = 2_000
_OPENCODE_BIN_OPTION_HELP = " ".join(
    [
        "Debug override for the OpenCode executable used by managed sessions",
        f"(defaults to {OPENCODE_BIN_ENV}, then `opencode` on PATH).",
    ]
)


class _OpenCodeLaunchError(Exception):
    """Raised when native OpenCode launch preparation fails."""


_OPENCODE_RUNTIME_PLUGIN = r"""
const SOURCE = "opencode_event"
const POST_TIMEOUT_MS = __LONGHOUSE_POST_TIMEOUT_MS__

function requireOption(options, name) {
  const value = options && typeof options[name] === "string" ? options[name].trim() : ""
  if (!value) throw new Error(`Longhouse OpenCode plugin missing ${name}`)
  return value
}

function phaseForStatus(status) {
  const type = status && typeof status.type === "string" ? status.type : ""
  if (type === "busy") return { phase: "running" }
  if (type === "retry") return { phase: "blocked", toolName: "retry" }
  return { phase: "idle" }
}

function buildEvent(ctx, kind, phase, toolName, payload) {
  const occurredAt = new Date().toISOString()
  ctx.seq += 1
  return {
    runtime_key: `opencode:${ctx.sessionID}`,
    session_id: ctx.sessionID,
    provider: "opencode",
    device_id: ctx.deviceID,
    source: SOURCE,
    kind,
    phase,
    tool_name: toolName || null,
    occurred_at: occurredAt,
    dedupe_key: `${ctx.sessionID}:${SOURCE}:${ctx.seq}:${payload && payload.eventID ? payload.eventID : occurredAt}`,
    payload: payload || {},
  }
}

async function postEvents(ctx, events) {
  if (!events.length) return
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), POST_TIMEOUT_MS)
  try {
    const response = await fetch(ctx.runtimeEventsUrl, {
      method: "POST",
      signal: controller.signal,
      headers: {
        "content-type": "application/json",
        "x-agents-token": ctx.token,
      },
      body: JSON.stringify({ events }),
    })
    if (!response.ok) {
      const body = await response.text().catch(() => "")
      console.warn(`Longhouse runtime ingest failed: ${response.status} ${body.slice(0, 200)}`)
    }
  } catch (error) {
    console.warn(`Longhouse runtime ingest failed: ${error && error.message ? error.message : error}`)
  } finally {
    clearTimeout(timeout)
  }
}

export default {
  id: "longhouse-runtime",
  async server(_input, options) {
    const ctx = {
      runtimeEventsUrl: requireOption(options, "runtimeEventsUrl"),
      token: requireOption(options, "token"),
      sessionID: requireOption(options, "longhouseSessionID"),
      deviceID: requireOption(options, "deviceID"),
      seq: 0,
    }

    return {
      async event({ event }) {
        const type = event && event.type
        const props = (event && event.properties) || {}
        if (type === "session.status") {
          const mapped = phaseForStatus(props.status)
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", mapped.phase, mapped.toolName, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              opencodeStatus: props.status,
            }),
          ])
        }
        if (type === "session.idle") {
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", "idle", null, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
            }),
          ])
        }
        if (type === "permission.asked") {
          const requestID = (props && (props.id || props.requestID || props.permissionID)) || null
          const toolName = (props && (props.tool || props.toolName)) || null
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", "blocked", "permission", {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              permission: props,
            }),
            // Also register an ANSWERABLE permission pause request so Longhouse
            // (web/iOS) can allow/deny and push the decision back via the bridge.
            buildEvent(ctx, "pause_request", null, toolName, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              request_id: requestID,
              provider_request_id: requestID,
              kind: "permission_prompt",
              can_respond: requestID ? true : false,
              provider_ref: { source: "opencode_bridge", reply_transport: "managed_push", opencode_request_id: requestID },
              tool_name: toolName,
              title: toolName ? ("Permission: " + toolName) : "Tool permission",
              summary: toolName ? ("OpenCode wants to use " + toolName) : "OpenCode is requesting tool permission.",
              permission: props,
            }),
          ])
        }
        if (type === "permission.replied") {
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", "running", null, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              permission: props,
            }),
          ])
        }
      },
      async "chat.message"(input) {
        await postEvents(ctx, [
          buildEvent(ctx, "phase_signal", "running", null, {
            hook: "chat.message",
            opencodeSessionID: input.sessionID,
            opencodeMessageID: input.messageID,
            agent: input.agent,
            model: input.model,
          }),
        ])
      },
      async "tool.execute.before"(input) {
        await postEvents(ctx, [
          buildEvent(ctx, "phase_signal", "running", input.tool, {
            hook: "tool.execute.before",
            opencodeSessionID: input.sessionID,
            opencodeCallID: input.callID,
            tool: input.tool,
          }),
        ])
      },
      async "tool.execute.after"(input) {
        await postEvents(ctx, [
          buildEvent(ctx, "phase_signal", "running", null, {
            hook: "tool.execute.after",
            opencodeSessionID: input.sessionID,
            opencodeCallID: input.callID,
            tool: input.tool,
          }),
        ])
      },
    }
  },
}
""".strip().replace("__LONGHOUSE_POST_TIMEOUT_MS__", str(_OPENCODE_RUNTIME_PLUGIN_POST_TIMEOUT_MS))


def _resolve_explicit_opencode_binary(candidate: str, *, source: str) -> str:
    normalized = str(candidate or "").strip()
    if not normalized:
        raise _OpenCodeLaunchError(f"{source} is empty")
    looks_like_path = normalized.startswith((".", "~", "/")) or "/" in normalized or "\\" in normalized
    if looks_like_path:
        path = Path(os.path.expanduser(normalized))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise _OpenCodeLaunchError(f"{source} points to `{candidate}`, but it is not an executable file.")
    resolved = shutil.which(normalized)
    if resolved:
        return resolved
    raise _OpenCodeLaunchError(f"{source} points to `{candidate}`, but it was not found on PATH.")


def _resolve_opencode_binary(explicit: str | None = None) -> str | None:
    normalized = str(explicit or "").strip()
    if normalized:
        return _resolve_explicit_opencode_binary(normalized, source=PROVIDER_CLI_SOURCE_OPENCODE_BIN_FLAG)
    env_candidate = str(os.environ.get(OPENCODE_BIN_ENV) or "").strip()
    if env_candidate:
        return _resolve_explicit_opencode_binary(env_candidate, source=OPENCODE_BIN_ENV)
    return shutil.which("opencode")


def _opencode_binary_source(explicit: str | None, resolved: str | None) -> str:
    if str(explicit or "").strip():
        return PROVIDER_CLI_SOURCE_OPENCODE_BIN_FLAG
    if str(os.environ.get(OPENCODE_BIN_ENV) or "").strip():
        return OPENCODE_BIN_ENV
    return PROVIDER_CLI_SOURCE_PATH if resolved else PROVIDER_CLI_SOURCE_MISSING


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
        provider="opencode",
    )


def _managed_runtime_events_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/agents/runtime/events/batch"


def _opencode_runtime_dir(config_dir: Path | None = None) -> Path:
    return get_managed_local_dir("opencode", base_dir=config_dir)


def _ensure_opencode_runtime_plugin(config_dir: Path | None = None) -> Path:
    runtime_dir = _opencode_runtime_dir(config_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    plugin_path = runtime_dir / _OPENCODE_RUNTIME_PLUGIN_FILENAME
    plugin_path.write_text(_OPENCODE_RUNTIME_PLUGIN + "\n", encoding="utf-8")
    return plugin_path


def _ensure_opencode_coordination_instructions(config_dir: Path | None = None) -> Path:
    runtime_dir = _opencode_runtime_dir(config_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / _OPENCODE_COORDINATION_INSTRUCTIONS_FILENAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(COORDINATION_INSTRUCTIONS)
    return path


def _opencode_coordination_enabled(*, session_id: str, device_id: str) -> bool:
    decision = evaluate_managed_provider_capability(
        capability_id="coordination.awareness.create",
        context=EvaluationContext(
            machine_id=device_id,
            session_id=session_id,
            provider="opencode",
            mode="helm",
            observed_at=datetime.now(timezone.utc),
            runtime=RuntimeState.READY,
        ),
    )
    return decision is not None and decision.action in {
        ProductAction.ENABLED,
        ProductAction.ENABLED_WITH_WARNING,
    }


def _opencode_config_content_with_longhouse_plugin(
    *,
    existing_content: str | None,
    plugin_path: Path,
    runtime_events_url: str,
    token: str,
    session_id: str,
    device_id: str,
    model: str | None = None,
    coordination_instructions_path: Path | None = None,
    longhouse_mcp_command: tuple[str, ...] | None = None,
) -> str:
    if existing_content and existing_content.strip():
        try:
            config = json.loads(existing_content)
        except json.JSONDecodeError as exc:
            raise _OpenCodeLaunchError("OPENCODE_CONFIG_CONTENT is set but is not valid JSON") from exc
        if not isinstance(config, dict):
            raise _OpenCodeLaunchError("OPENCODE_CONFIG_CONTENT must be a JSON object")
    else:
        config = {}

    plugins = config.get("plugin")
    if plugins is None:
        plugins = []
    if not isinstance(plugins, list):
        raise _OpenCodeLaunchError("OPENCODE_CONFIG_CONTENT plugin field must be an array")
    plugins = list(plugins)
    plugins.append(
        [
            plugin_path.resolve().as_uri(),
            {
                "runtimeEventsUrl": runtime_events_url,
                "token": token,
                "longhouseSessionID": session_id,
                "deviceID": device_id,
            },
        ]
    )
    config["plugin"] = plugins
    if coordination_instructions_path is not None:
        instructions = config.get("instructions", [])
        if not isinstance(instructions, list):
            raise _OpenCodeLaunchError("OPENCODE_CONFIG_CONTENT instructions field must be an array")
        instruction_path = str(coordination_instructions_path.resolve())
        config["instructions"] = [*instructions, instruction_path] if instruction_path not in instructions else instructions
    if longhouse_mcp_command is not None:
        mcp = config.get("mcp", {})
        if not isinstance(mcp, dict):
            raise _OpenCodeLaunchError("OPENCODE_CONFIG_CONTENT mcp field must be an object")
        config["mcp"] = {
            **mcp,
            "longhouse": {
                "type": "local",
                "command": list(longhouse_mcp_command),
                "enabled": True,
            },
        }
    normalized_model = str(model or "").strip()
    if normalized_model:
        config["model"] = normalized_model
    return json.dumps(config, separators=(",", ":"))


def _write_opencode_runtime_config_content(
    *,
    config_dir: Path | None,
    runtime_events_url: str,
    token: str,
    session_id: str,
    device_id: str,
    model: str | None = None,
) -> Path:
    plugin_path = _ensure_opencode_runtime_plugin(config_dir)
    coordination_enabled = _opencode_coordination_enabled(session_id=session_id, device_id=device_id)
    instructions_path = _ensure_opencode_coordination_instructions(config_dir) if coordination_enabled else None
    api_url = runtime_events_url.partition("/api/agents/")[0]
    content = _opencode_config_content_with_longhouse_plugin(
        existing_content=os.environ.get("OPENCODE_CONFIG_CONTENT"),
        plugin_path=plugin_path,
        runtime_events_url=runtime_events_url,
        token=token,
        session_id=session_id,
        device_id=device_id,
        model=model,
        coordination_instructions_path=instructions_path,
        longhouse_mcp_command=("longhouse", "mcp-server", "--url", api_url) if coordination_enabled else None,
    )
    runtime_dir = _opencode_runtime_dir(config_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    config_path = runtime_dir / f"{session_id}.config-content.json"
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content + "\n")
    return config_path


def _split_opencode_root_options(opencode_args: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
    """Return attach-safe args plus root-only model selection.

    Managed OpenCode runs `opencode serve` and then `opencode attach`. The
    upstream `-m/--model` option belongs to the root TUI command and is not
    accepted by either subcommand, so Longhouse carries it through config.
    """

    attach_args: list[str] = []
    model: str | None = None
    i = 0
    while i < len(opencode_args):
        arg = str(opencode_args[i])
        if arg in ("-m", "--model"):
            if i + 1 >= len(opencode_args) or str(opencode_args[i + 1]).startswith("-"):
                raise _OpenCodeLaunchError("OpenCode model flag requires a provider/model value.")
            model = str(opencode_args[i + 1])
            i += 2
            continue
        if arg.startswith("--model="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise _OpenCodeLaunchError("OpenCode model flag requires a provider/model value.")
            model = value
            i += 1
            continue
        attach_args.append(arg)
        i += 1
    return tuple(attach_args), model


def _write_opencode_launch_script(
    *,
    config_dir: Path | None,
    session_id: str,
    device_id: str,
    opencode_bin: str,
    cwd: Path,
    runtime_events_url: str,
    token: str,
    config_content_path: Path,
) -> Path:
    runtime_dir = _opencode_runtime_dir(config_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    script_path = runtime_dir / f"{session_id}.launch.sh"
    script = f"""#!/bin/sh
export LONGHOUSE_MANAGED_SESSION_ID={shlex.quote(session_id)}
export LONGHOUSE_DEVICE_ID={shlex.quote(device_id)}
export OPENCODE_CONFIG_CONTENT="$(cat {shlex.quote(str(config_content_path))})"
cd {shlex.quote(str(cwd))} || exit 1
{shlex.quote(opencode_bin)} "$@"
status=$?
LONGHOUSE_RUNTIME_STATUS="$status" \\
LONGHOUSE_RUNTIME_EVENTS_URL={shlex.quote(runtime_events_url)} \\
LONGHOUSE_RUNTIME_TOKEN={shlex.quote(token)} \\
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
    "runtime_key": f"opencode:{{session_id}}",
    "session_id": session_id,
    "provider": "opencode",
    "device_id": os.environ["LONGHOUSE_RUNTIME_DEVICE_ID"],
    "source": "opencode_event",
    "kind": "terminal_signal",
    "phase": "finished",
    "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "dedupe_key": f"{{session_id}}:opencode_event:terminal:{{status}}",
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
        "runtime_key": f"opencode:{session_id}",
        "session_id": session_id,
        "provider": "opencode",
        "device_id": device_id,
        "source": _OPENCODE_RUNTIME_SOURCE,
        "kind": kind,
        "phase": phase,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dedupe_key": dedupe_key,
        "payload": payload,
    }


def _post_opencode_runtime_event(
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
        with urlopen(request, timeout=_OPENCODE_RUNTIME_EVENT_TIMEOUT_SECONDS) as response:
            response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise _OpenCodeLaunchError(f"Could not send OpenCode runtime event to Longhouse: {exc}") from exc


def _build_opencode_command(
    *,
    session_id: str,
    machine_name: str,
    opencode_bin: str,
    cwd: Path,
    opencode_args: tuple[str, ...],
    config_content_path: Path | None = None,
    launch_script_path: Path | None = None,
) -> str:
    if launch_script_path is not None:
        command = " ".join([shlex.quote(str(launch_script_path)), *(shlex.quote(arg) for arg in opencode_args)])
        return f"cd {shlex.quote(str(cwd))} && {command}"

    env_items = [
        f"LONGHOUSE_MANAGED_SESSION_ID={shlex.quote(session_id)}",
        f"LONGHOUSE_DEVICE_ID={shlex.quote(machine_name)}",
    ]
    if config_content_path is not None:
        env_items.append(f'OPENCODE_CONFIG_CONTENT="$(cat {shlex.quote(str(config_content_path))})"')
    env_prefix = " ".join(env_items)
    command = " ".join([shlex.quote(opencode_bin), *(shlex.quote(arg) for arg in opencode_args)])
    return f"cd {shlex.quote(str(cwd))} && {env_prefix} {command}"


_OPENCODE_DEFAULT_SERVE_ARGS: tuple[str, ...] = ("serve", "--port", "0", "--hostname", "127.0.0.1")
_OPENCODE_LISTEN_TIMEOUT_SECS = 30.0


def _ensure_managed_serve_args(opencode_args: tuple[str, ...]) -> tuple[str, ...]:
    """Coerce caller args so the managed launch always lands on `opencode serve`.

    Longhouse owns the control plane: managed-local OpenCode means an HTTP
    server we can drive from `longhouse opencode-bridge`. If the user passed
    no args, default to `serve --port 0 --hostname 127.0.0.1`. If they passed
    a different subcommand (e.g. `tui`), refuse loudly so we never end up in
    "managed but unsteerable" land.
    """

    if not opencode_args:
        return _OPENCODE_DEFAULT_SERVE_ARGS
    first = (opencode_args[0] or "").strip()
    if first.startswith("-") or first == "":
        # No subcommand — caller is passing flags, prepend the canonical serve.
        return _OPENCODE_DEFAULT_SERVE_ARGS + opencode_args
    if first != "serve":
        raise _OpenCodeLaunchError(
            "Managed `longhouse opencode` only supports the `serve` subcommand "
            "(it is the upstream HTTP control plane). To attach an interactive "
            "TUI, run `opencode tui attach <url>` against the server URL printed "
            "above using $OPENCODE_SERVER_PASSWORD."
        )
    return opencode_args


def _stream_and_capture_listen_url(
    process: subprocess.Popen,
    *,
    on_url: "callable[[str], None]",
    timeout_secs: float = _OPENCODE_LISTEN_TIMEOUT_SECS,
) -> threading.Thread:
    """Tee the child's stdout to ours and call ``on_url`` once the listen line appears.

    Returns the reader thread (caller can ``join`` once the child exits).
    The caller is responsible for closing the child's stdout when done.
    """

    captured = {"done": False}

    def _reader() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except Exception:
                pass
            if not captured["done"]:
                url = parse_listen_line(line)
                if url:
                    captured["done"] = True
                    try:
                        on_url(url)
                    except Exception as exc:  # pragma: no cover - defensive
                        typer.secho(
                            f"Longhouse: failed to record opencode bridge state: {exc}",
                            fg=typer.colors.YELLOW,
                            err=True,
                        )

    thread = threading.Thread(target=_reader, name="opencode-stdout-tee", daemon=True)
    thread.start()
    return thread


def _run_native_opencode(
    *,
    session_id: str,
    machine_name: str,
    opencode_bin: str,
    cwd: Path,
    opencode_args: tuple[str, ...],
    url: str,
    token: str,
    config_dir: Path | None = None,
    opencode_bin_source: str | None = None,
) -> int:
    serve_args = _ensure_managed_serve_args(opencode_args)
    cmd = [opencode_bin, *serve_args]
    env = os.environ.copy()
    env["LONGHOUSE_MANAGED_SESSION_ID"] = session_id
    add_interactive_human_shell_launch_env(env)
    env["LONGHOUSE_DEVICE_ID"] = machine_name
    server_password = generate_server_password()
    env["OPENCODE_SERVER_PASSWORD"] = server_password
    env.setdefault("OPENCODE_SERVER_USERNAME", "opencode")
    plugin_path = _ensure_opencode_runtime_plugin(config_dir)
    coordination_enabled = _opencode_coordination_enabled(session_id=session_id, device_id=machine_name)
    instructions_path = _ensure_opencode_coordination_instructions(config_dir) if coordination_enabled else None
    env["OPENCODE_CONFIG_CONTENT"] = _opencode_config_content_with_longhouse_plugin(
        existing_content=env.get("OPENCODE_CONFIG_CONTENT"),
        plugin_path=plugin_path,
        runtime_events_url=_managed_runtime_events_url(url),
        token=token,
        session_id=session_id,
        device_id=machine_name,
        coordination_instructions_path=instructions_path,
        longhouse_mcp_command=("longhouse", "mcp-server", "--url", url) if coordination_enabled else None,
    )

    def _record_state(server_url: str) -> None:
        state_path = write_opencode_bridge_state(
            session_id=session_id,
            server_url=server_url,
            server_password=server_password,
            server_username=env.get("OPENCODE_SERVER_USERNAME", "opencode"),
            cwd=str(cwd),
            opencode_pid=process.pid,
            config_dir=config_dir,
            ready=True,
        )
        try:
            record_managed_provider_contract(
                provider="opencode",
                session_id=session_id,
                cwd=cwd,
                config_dir=config_dir,
                launch_mode="serve_attached",
                provider_binary_path=opencode_bin,
                provider_binary_source=opencode_bin_source,
                control_kind="opencode_bridge",
                control_state_path=state_path,
            )
        except Exception as exc:  # pragma: no cover - defensive warning path
            typer.secho(
                f"Longhouse warning: could not record managed-session contract: {exc}",
                fg=typer.colors.YELLOW,
                err=True,
            )

    returncode = 1
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        reader = _stream_and_capture_listen_url(process, on_url=_record_state)
        try:
            returncode = int(process.wait())
        except KeyboardInterrupt:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                returncode = int(process.wait(timeout=5))
            except Exception:
                returncode = 130
            raise
        finally:
            # Drain reader before letting the stdout pipe close — once the
            # child exits, the reader's iterator drops out naturally on EOF.
            # Bound the join so a misbehaving stdout never wedges the launcher.
            reader.join(timeout=2.0)
        return returncode
    finally:
        remove_managed_provider_contract(provider="opencode", session_id=session_id, config_dir=config_dir)
        try:
            remove_opencode_bridge_state(session_id=session_id, config_dir=config_dir)
        except Exception:
            pass
        try:
            _post_opencode_runtime_event(
                url=url,
                token=token,
                event=_runtime_event_payload(
                    session_id=session_id,
                    device_id=machine_name,
                    kind="terminal_signal",
                    phase="finished",
                    dedupe_key=f"{session_id}:{_OPENCODE_RUNTIME_SOURCE}:terminal:{returncode}",
                    payload={"terminal_state": "session_ended", "exit_code": returncode},
                ),
            )
        except _OpenCodeLaunchError as exc:
            typer.secho(f"Longhouse runtime event warning: {exc}", fg=typer.colors.YELLOW, err=True)


def opencode(
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
    name: str | None = typer.Option(None, "--name", help="Optional display name for the OpenCode session."),
    attach: bool = typer.Option(
        True,
        "--attach/--no-attach",
        help="Launch OpenCode after creating the Longhouse session when running interactively.",
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
    opencode_bin: str | None = typer.Option(
        None,
        "--opencode-bin",
        help=_OPENCODE_BIN_OPTION_HELP,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose/--quiet",
        "-v",
        help="Show full session id, timeline URL, and attach command.",
    ),
) -> None:
    """Launch a Longhouse OpenCode session on this machine.

    Extra arguments after the Longhouse options are passed to the stock
    `opencode` executable.
    """

    launch_ui.quiet_diagnostic_logs(verbose)
    try:
        opencode_args, model = _split_opencode_root_options(tuple(str(arg) for arg in (ctx.args or ())))
    except _OpenCodeLaunchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    resolved_config_dir = Path(config_dir) if config_dir else None
    resolved_url, resolved_token = _load_api_credentials(
        url=url,
        token=token,
        config_dir=resolved_config_dir,
        exit_code=managed_local_cli.EXIT_SETUP_FAILED,
    )
    try:
        resolved_opencode_bin = _resolve_opencode_binary(opencode_bin)
    except _OpenCodeLaunchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if not resolved_opencode_bin:
        typer.secho(
            "OpenCode executable not found. Install OpenCode so `opencode` is on PATH, "
            f"or set {OPENCODE_BIN_ENV} / --opencode-bin explicitly for debugging.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    machine_name = get_machine_name_label()
    _ensure_managed_launch_preflight(
        url=resolved_url,
        machine_name=machine_name,
        config_dir=resolved_config_dir,
        config_dir_is_provider_home=False,
        exit_code=managed_local_cli.EXIT_SETUP_FAILED,
    )
    launch_ui.progress("Preparing your session…")
    if verbose:
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

    is_interactive = _interactive_stdio()

    from zerg.cli.opencode_channel import LAUNCH_MODE_DETACHED
    from zerg.cli.opencode_channel import LAUNCH_MODE_KEEP_SERVER
    from zerg.cli.opencode_channel import OpenCodeServerBridgeError
    from zerg.cli.opencode_channel import _install_opencode_signal_cleanup
    from zerg.cli.opencode_channel import _restore_signal_handlers
    from zerg.cli.opencode_channel import launch_opencode_server_bridge
    from zerg.cli.opencode_channel import run_opencode_attach

    will_attach = attach and is_interactive
    if will_attach:
        launch_mode = LAUNCH_MODE_KEEP_SERVER
    else:
        launch_mode = LAUNCH_MODE_DETACHED

    launch_ui.progress("Starting native OpenCode bridge…")
    try:
        launch_opencode_server_bridge(
            session_id=result.session_id,
            run_id=result.run_id,
            cwd=cwd,
            api_url=resolved_url,
            api_token=resolved_token,
            device_id=machine_name,
            display_name=name,
            opencode_bin=resolved_opencode_bin,
            config_dir=resolved_config_dir,
            launch_mode=launch_mode,
            owner_wrapper_pid=None,
            model=model,
        )
    except (_OpenCodeLaunchError, OpenCodeServerBridgeError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    attach_command = f"longhouse opencode-channel attach --session-id {result.session_id}"
    # Bridge is up (it waits for health) — only now is the session steerable.
    launch_ui.launch_panel(
        provider_label=launch_ui.PROVIDER_LABELS["opencode"],
        base_url=resolved_url,
        machine_name=machine_name,
        session_id=result.session_id,
        verbose=verbose,
        attach_command=attach_command,
    )

    if open_browser:
        typer.echo("Opening session in browser...")
        if not _open_session_url(session_url):
            typer.secho(f"Could not open browser automatically. Visit: {session_url}", fg=typer.colors.YELLOW)

    if not attach:
        typer.secho(
            "Server left running for reattach (use `longhouse opencode-channel stop` to end it).",
            fg=typer.colors.YELLOW,
        )
        typer.echo(f"Run: {attach_command}")
        return
    if not is_interactive:
        typer.secho(
            "Skipping OpenCode attach because stdin/stdout are not TTYs; " "server left running for reattach.",
            fg=typer.colors.YELLOW,
        )
        typer.echo(f"Run: {attach_command}")
        return

    launch_ui.progress("Attaching OpenCode…")
    # The attach TUI is a client, not the execution owner. Its exit or failure
    # must never terminate the managed provider server.
    previous_handlers = _install_opencode_signal_cleanup()
    exit_code = 1
    try:
        exit_code = run_opencode_attach(
            session_id=result.session_id,
            opencode_bin=resolved_opencode_bin,
            config_dir=resolved_config_dir,
            extra_args=opencode_args,
        )
    except KeyboardInterrupt:
        exit_code = 130
        raise
    finally:
        _restore_signal_handlers(previous_handlers)

    if exit_code == 0:
        typer.secho("🔥  The hearth still burns — terminal detached.", fg=typer.colors.YELLOW)
        typer.echo(f"   Rejoin: {attach_command}")
        typer.echo(f"   Stop: longhouse opencode-channel stop --session-id {result.session_id}")
    else:
        launch_ui.exit_bookend(
            exit_code=exit_code,
            machine_name=machine_name,
            reattach_command=attach_command,
            reattachable_on_nonzero_exit=True,
        )
        typer.echo(f"   Stop: longhouse opencode-channel stop --session-id {result.session_id}")
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
