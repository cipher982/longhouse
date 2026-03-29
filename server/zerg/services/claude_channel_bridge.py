"""Helpers for the native Claude channel bridge transport."""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any

from zerg.services.managed_local_tmux import build_managed_local_shell_prelude

CLAUDE_CHANNEL_SERVER_NAME = "longhouse-channel"


def _quote(value: str) -> str:
    return shlex.quote(value)


def _resolve_claude_dir(claude_dir: str | Path | None = None) -> Path:
    if claude_dir is None:
        return Path.home() / ".claude"
    return Path(claude_dir).expanduser()


def resolve_claude_user_config_path(*, claude_dir: str | Path | None = None) -> Path:
    resolved_dir = _resolve_claude_dir(claude_dir)
    return resolved_dir.parent / f"{resolved_dir.name}.json"


def resolve_claude_project_key(workspace_path: str | Path) -> str:
    workspace = Path(workspace_path).expanduser().resolve()
    return str(workspace)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _default_project_local_scope_entry() -> dict[str, Any]:
    return {
        "allowedTools": [],
        "mcpContextUris": [],
        "mcpServers": {},
        "enabledMcpjsonServers": [],
        "disabledMcpjsonServers": [],
        "hasTrustDialogAccepted": False,
        "projectOnboardingSeenCount": 0,
        "hasClaudeMdExternalIncludesApproved": False,
        "hasClaudeMdExternalIncludesWarningShown": False,
    }


def install_claude_channel_mcp_server(
    *,
    workspace_path: str | Path,
    claude_dir: str | Path | None = None,
    command: str = "longhouse",
    args: list[str] | None = None,
) -> list[str]:
    """Ensure the Longhouse Claude channel MCP server is registered in Claude's local project config."""

    user_config_path = resolve_claude_user_config_path(claude_dir=claude_dir)
    settings = _read_json_object(user_config_path)
    actions: list[str] = []
    project_key = resolve_claude_project_key(workspace_path)

    desired = {
        "type": "stdio",
        "command": command,
        "args": list(args or ["claude-channel", "serve"]),
        "env": {},
    }

    projects = settings.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects = {}
        settings["projects"] = projects

    project_settings = projects.get(project_key)
    if not isinstance(project_settings, dict):
        project_settings = _default_project_local_scope_entry()
        projects[project_key] = project_settings
    else:
        project_settings.setdefault("mcpServers", {})

    mcp_servers = project_settings.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        project_settings["mcpServers"] = mcp_servers
    current = mcp_servers.get(CLAUDE_CHANNEL_SERVER_NAME)
    if current != desired:
        mcp_servers[CLAUDE_CHANNEL_SERVER_NAME] = desired
        _write_json_object(user_config_path, settings)
        actions.append(f"Updated {user_config_path} with local MCP server {CLAUDE_CHANNEL_SERVER_NAME} for {project_key}")

    return actions


def resolve_claude_channel_state_root(
    *,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> Path:
    if state_root is not None:
        return Path(state_root).expanduser()
    return _resolve_claude_dir(claude_dir) / "channels" / "longhouse"


def build_claude_channel_state_file(
    *,
    session_id: str,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> Path:
    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("session_id must not be empty")
    return resolve_claude_channel_state_root(state_root=state_root, claude_dir=claude_dir) / "sessions" / f"{normalized}.json"


def read_claude_channel_state(
    *,
    session_id: str,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> dict[str, Any]:
    state_path = build_claude_channel_state_file(session_id=session_id, state_root=state_root, claude_dir=claude_dir)
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Claude channel state at {state_path} is not a JSON object")
    return raw


def wait_for_claude_channel_state(
    *,
    session_id: str,
    timeout_secs: float = 10.0,
    poll_interval_secs: float = 0.1,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_secs
    state_path = build_claude_channel_state_file(session_id=session_id, state_root=state_root, claude_dir=claude_dir)
    while time.monotonic() < deadline:
        if state_path.exists():
            return read_claude_channel_state(session_id=session_id, state_root=state_root, claude_dir=claude_dir)
        time.sleep(poll_interval_secs)
    raise FileNotFoundError(f"Claude channel state did not appear at {state_path} within {timeout_secs:.1f}s")


def build_claude_channel_exec_command(
    *,
    provider_session_id: str,
    longhouse_session_id: str,
    cwd: str,
    resume: bool,
    hook_url: str | None = None,
    hook_token: str | None = None,
    claude_command: str = "claude-code",
) -> str:
    """Build the native Claude launch/resume shell command for channel sessions."""

    provider_sid = str(provider_session_id or "").strip()
    longhouse_sid = str(longhouse_session_id or "").strip()
    working_dir = str(cwd or "").strip()
    if not provider_sid:
        raise ValueError("provider_session_id must not be empty")
    if not longhouse_sid:
        raise ValueError("longhouse_session_id must not be empty")
    if not working_dir:
        raise ValueError("cwd must not be empty")

    target_flag = "--resume" if resume else "--session-id"
    command_bits = [
        claude_command,
        target_flag,
        provider_sid,
        "--dangerously-load-development-channels",
        f"server:{CLAUDE_CHANNEL_SERVER_NAME}",
    ]
    inner = [
        build_managed_local_shell_prelude(require_tmux=False, required_commands=(claude_command,)),
        f"cd {_quote(working_dir)}",
        f"export LONGHOUSE_SESSION_ID={_quote(longhouse_sid)}",
        f"export LONGHOUSE_CHANNEL_SESSION_ID={_quote(longhouse_sid)}",
        f"export LONGHOUSE_PROVIDER_SESSION_ID={_quote(provider_sid)}",
    ]
    if hook_url:
        inner.append(f"export LONGHOUSE_HOOK_URL={_quote(str(hook_url).strip())}")
    if hook_token:
        inner.append(f"export LONGHOUSE_HOOK_TOKEN={_quote(str(hook_token).strip())}")
    inner.append("exec " + " ".join(_quote(part) for part in command_bits))
    return f"zsh -lc {_quote('; '.join(inner))}"


__all__ = [
    "CLAUDE_CHANNEL_SERVER_NAME",
    "build_claude_channel_exec_command",
    "build_claude_channel_state_file",
    "install_claude_channel_mcp_server",
    "read_claude_channel_state",
    "resolve_claude_project_key",
    "resolve_claude_channel_state_root",
    "resolve_claude_user_config_path",
    "wait_for_claude_channel_state",
]
