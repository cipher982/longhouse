"""Claude Code hook installation and MCP server registration for Longhouse.

Installs hook scripts and injects hook configuration into
~/.claude/settings.json so that Claude Code automatically ships
sessions and displays recent session context.

Also provides MCP server registration for both Claude Code (~/.claude.json)
and Codex CLI (~/.codex/config.toml).

Two hooks are installed:

- **Stop** (``longhouse-ship.sh``): After each assistant response, ships
  the session transcript to Longhouse asynchronously.
- **SessionStart** (``longhouse-session-start.sh``): On fresh session
  startup, queries Longhouse for recent sessions in the current project
  and injects a system message with context.

Usage:
    from zerg.services.shipper.hooks import install_hooks

    actions = install_hooks(url="https://david.longhouse.ai")
    for action in actions:
        print(action)
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hook script templates
# ---------------------------------------------------------------------------

SHIP_HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse Stop hook — ships session transcript after each response
# Installed by: longhouse connect --install
INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [[ -z "$TRANSCRIPT" ]] || [[ ! -f "$TRANSCRIPT" ]]; then
    exit 0
fi
exec __ENGINE_PATH__ ship --file "$TRANSCRIPT" --quiet 2>/dev/null
"""

PRESENCE_HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse presence hook — emits real-time session state on each lifecycle event
# Installed by: longhouse connect --install
# Registered on: UserPromptSubmit, PreToolUse, PostToolUse, Stop
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')

[ -z "$SESSION_ID" ] && exit 0

# Map event → presence state
case "$EVENT" in
  UserPromptSubmit) STATE="thinking" ;;
  PreToolUse)       STATE="running" ;;
  PostToolUse|PostToolUseFailure) STATE="thinking" ;;
  Stop)             STATE="idle" ;;
  *) exit 0 ;;
esac

TOKEN_FILE="$HOME/.claude/longhouse-device-token"
URL_FILE="$HOME/.claude/longhouse-url"
[ ! -f "$TOKEN_FILE" ] || [ ! -f "$URL_FILE" ] && exit 0
TOKEN=$(cat "$TOKEN_FILE" | tr -d '[:space:]')
URL=$(cat "$URL_FILE" | tr -d '[:space:]')
[ -z "$TOKEN" ] || [ -z "$URL" ] && exit 0

curl -sf -X POST --max-time 2 \\
  -H "X-Agents-Token: $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d "{\\"session_id\\":\\"$SESSION_ID\\",\\"state\\":\\"$STATE\\",\\"tool_name\\":\\"$TOOL\\",\\"cwd\\":\\"$CWD\\"}" \\
  "${URL}/api/agents/presence" 2>/dev/null
exit 0
"""

SESSION_START_HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse SessionStart hook — shows recent sessions on new session
# Installed by: longhouse connect --install
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
SOURCE=$(echo "$INPUT" | jq -r '.source // empty')

# Only fire on fresh session start (not resume/compact)
if [[ "$SOURCE" != "startup" ]]; then exit 0; fi

PROJECT=$(basename "$CWD")
if [[ -z "$PROJECT" ]]; then exit 0; fi

TOKEN_FILE="$HOME/.claude/longhouse-device-token"
URL_FILE="$HOME/.claude/longhouse-url"
if [[ ! -f "$TOKEN_FILE" ]] || [[ ! -f "$URL_FILE" ]]; then exit 0; fi
TOKEN=$(cat "$TOKEN_FILE" | tr -d '[:space:]')
URL=$(cat "$URL_FILE" | tr -d '[:space:]')

RESPONSE=$(curl -sf --max-time 4 \\
  -H "X-Agents-Token: $TOKEN" \\
  "${URL}/api/agents/sessions?project=${PROJECT}&limit=5&days_back=7" 2>/dev/null)
if [[ $? -ne 0 ]] || [[ -z "$RESPONSE" ]]; then exit 0; fi

TOTAL=$(echo "$RESPONSE" | jq -r '.total // 0')
if [[ "$TOTAL" -eq 0 ]]; then exit 0; fi

LINES=$(echo "$RESPONSE" | jq -r '.sessions[:5][] | "  \\(.started_at | split("T")[0]) \\(.provider // "?") \\(.project // "?") (\\(.tool_calls // 0) tools)"')
MSG="Longhouse: ${TOTAL} sessions in ${PROJECT} (7d):\\n${LINES}"

jq -nc --arg msg "$MSG" '{"systemMessage": $msg}'
exit 0
"""

# Marker used to identify Longhouse hooks inside settings.json so we can
# update in place rather than blindly appending duplicates.  Use the path
# prefix "longhouse-" which is specific enough to avoid false positives on
# user hooks that happen to mention "longhouse" in a description.
_HOOK_MARKER = "longhouse-"


def _make_hook_entries(hooks_dir: Path) -> tuple[dict, dict, dict]:
    """Build hook entry dicts with resolved script paths."""
    ship_path = str(hooks_dir / "longhouse-ship.sh")
    session_start_path = str(hooks_dir / "longhouse-session-start.sh")
    presence_path = str(hooks_dir / "longhouse-presence.sh")

    # Stop: ship transcript AND signal presence=idle, both async
    stop_entry = {
        "hooks": [
            {"type": "command", "command": ship_path, "async": True, "timeout": 30},
            {"type": "command", "command": presence_path, "async": True, "timeout": 5},
        ],
    }
    session_start_entry = {
        "hooks": [
            {
                "type": "command",
                "command": session_start_path,
                "async": False,
                "timeout": 5,
            }
        ],
    }
    # Presence-only entry for non-Stop events (ship is not needed there)
    presence_entry = {
        "hooks": [
            {
                "type": "command",
                "command": presence_path,
                "async": True,
                "timeout": 5,
            }
        ],
    }
    return stop_entry, session_start_entry, presence_entry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_claude_dir(claude_dir: str | None = None) -> Path:
    """Resolve the Claude config directory."""
    if claude_dir:
        return Path(claude_dir).expanduser()
    env_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".claude"


def _is_longhouse_hook(entry: dict) -> bool:
    """Return True if a hook entry belongs to Longhouse.

    Checks whether any inner hook's ``command`` field contains the
    marker string so we can update it in place.
    """
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "")
        if _HOOK_MARKER in cmd:
            return True
    return False


def _merge_hooks_for_event(
    existing_entries: list[dict],
    new_entry: dict,
) -> list[dict]:
    """Merge a Longhouse hook entry into an existing list for one event.

    If a Longhouse hook already exists in the list it is replaced;
    otherwise the new entry is appended. Non-Longhouse hooks are left
    untouched.

    Args:
        existing_entries: Current list of hook entries for the event.
        new_entry: The Longhouse hook entry to upsert.

    Returns:
        Updated list of hook entries.
    """
    updated = False
    result: list[dict] = []
    for entry in existing_entries:
        if _is_longhouse_hook(entry):
            # Replace existing Longhouse hook with the new one
            result.append(new_entry)
            updated = True
        else:
            result.append(entry)

    if not updated:
        result.append(new_entry)

    return result


def _read_settings(settings_path: Path) -> dict:
    """Read and parse settings.json, returning an empty dict if file is absent.

    Raises on parse errors to avoid silently clobbering a corrupted but
    recoverable settings file.
    """
    if not settings_path.exists():
        return {}
    text = settings_path.read_text()
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse {settings_path}: {exc}. Fix or remove the file manually before installing hooks.") from exc


def _write_settings(settings_path: Path, data: dict) -> None:
    """Write settings dict back to settings.json with indent=2."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_hooks(
    url: str,
    token: str | None = None,
    claude_dir: str | None = None,
    engine_path: str | None = None,
) -> list[str]:
    """Install Longhouse hook scripts and inject them into settings.json.

    This function is idempotent — running it multiple times updates
    existing hooks rather than creating duplicates.

    Steps performed:
    1. Create ``~/.claude/hooks/`` directory.
    2. Write ``longhouse-ship.sh`` and ``longhouse-session-start.sh``
       with executable permissions.
    3. Read ``~/.claude/settings.json`` (or start with ``{}``).
    4. Upsert Longhouse hook entries into the ``hooks`` object.
    5. Write ``settings.json`` back.

    Args:
        url: Longhouse API URL (used only for logging; the scripts
             read ``~/.claude/longhouse-url`` at runtime).
        token: Device token (unused by this function; scripts read
               ``~/.claude/longhouse-device-token`` at runtime).
        claude_dir: Override for Claude config directory.

    Returns:
        List of human-readable action strings describing what was done.
    """
    config_dir = _resolve_claude_dir(claude_dir)
    hooks_dir = config_dir / "hooks"
    settings_path = config_dir / "settings.json"
    actions: list[str] = []

    # ------------------------------------------------------------------
    # 1. Create hooks directory
    # ------------------------------------------------------------------
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 2. Write hook scripts (with resolved config dir so token/url reads
    #    point to the right place even when --claude-dir is used)
    # ------------------------------------------------------------------
    resolved_dir = str(config_dir)

    # Resolve engine path at install time and bake it into the hook.
    # exec replaces the shell process — zero Python overhead on every stop.
    if engine_path is None:
        try:
            from zerg.services.shipper.service import get_engine_executable

            engine_path = get_engine_executable()
        except RuntimeError:
            engine_path = "longhouse-engine"  # last resort: rely on PATH

    ship_script_content = SHIP_HOOK_SCRIPT.replace(
        "$HOME/.claude/",
        f"{resolved_dir}/",
    ).replace(
        "__ENGINE_PATH__",
        engine_path,
    )
    session_start_script_content = SESSION_START_HOOK_SCRIPT.replace(
        "$HOME/.claude/",
        f"{resolved_dir}/",
    )
    presence_script_content = PRESENCE_HOOK_SCRIPT.replace(
        "$HOME/.claude/",
        f"{resolved_dir}/",
    )

    ship_script = hooks_dir / "longhouse-ship.sh"
    ship_script.write_text(ship_script_content)
    ship_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    actions.append(f"Wrote {ship_script}")

    session_start_script = hooks_dir / "longhouse-session-start.sh"
    session_start_script.write_text(session_start_script_content)
    session_start_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    actions.append(f"Wrote {session_start_script}")

    presence_script = hooks_dir / "longhouse-presence.sh"
    presence_script.write_text(presence_script_content)
    presence_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    actions.append(f"Wrote {presence_script}")

    # ------------------------------------------------------------------
    # 3. Read existing settings
    # ------------------------------------------------------------------
    settings = _read_settings(settings_path)

    # ------------------------------------------------------------------
    # 4. Merge hook entries (using resolved absolute paths)
    # ------------------------------------------------------------------
    stop_entry, session_start_entry, presence_entry = _make_hook_entries(hooks_dir)
    hooks_obj = settings.setdefault("hooks", {})

    # Stop hook (ships transcript)
    stop_list = hooks_obj.get("Stop", [])
    hooks_obj["Stop"] = _merge_hooks_for_event(stop_list, stop_entry)

    # SessionStart hook (shows recent sessions)
    session_start_list = hooks_obj.get("SessionStart", [])
    hooks_obj["SessionStart"] = _merge_hooks_for_event(session_start_list, session_start_entry)

    # Presence-only hooks on the non-Stop events (Stop already has presence via stop_entry)
    for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        raw = hooks_obj.get(event, [])
        # Normalize: older Claude Code versions or manual edits may store a dict instead of list
        event_list = raw if isinstance(raw, list) else []
        hooks_obj[event] = _merge_hooks_for_event(event_list, presence_entry)

    # ------------------------------------------------------------------
    # 5. Write settings back
    # ------------------------------------------------------------------
    _write_settings(settings_path, settings)
    actions.append(f"Updated {settings_path} with Stop, SessionStart, and presence hooks")

    logger.info("Installed Longhouse hooks in %s", config_dir)
    return actions


def install_mcp_server(claude_dir: str | None = None) -> list[str]:
    """Register the Longhouse MCP server in ``~/.claude.json``.

    Claude Code reads MCP server configuration from ``~/.claude.json``
    (the top-level user config, *not* ``~/.claude/settings.json``).

    This function is idempotent — it adds or updates the
    ``mcpServers.longhouse`` entry while preserving all other settings.

    Args:
        claude_dir: Override for Claude config directory (only used to
                    locate the parent; the file is always
                    ``~/.claude.json`` unless overridden for testing).

    Returns:
        List of human-readable action strings describing what was done.
    """
    actions: list[str] = []

    if claude_dir:
        # For testing: place claude.json inside the provided dir
        claude_json_path = Path(claude_dir).expanduser() / "claude.json"
    else:
        claude_json_path = Path.home() / ".claude.json"

    # ------------------------------------------------------------------
    # 1. Read existing config
    # ------------------------------------------------------------------
    config: dict = {}
    if claude_json_path.exists():
        text = claude_json_path.read_text()
        if text.strip():
            try:
                config = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Failed to parse {claude_json_path}: {exc}. " "Fix or remove the file manually before registering MCP server."
                ) from exc

    # ------------------------------------------------------------------
    # 2. Add/update mcpServers.longhouse
    # ------------------------------------------------------------------
    mcp_servers = config.setdefault("mcpServers", {})
    mcp_servers["longhouse"] = {
        "type": "stdio",
        "command": "longhouse",
        "args": ["mcp-server"],
    }

    # ------------------------------------------------------------------
    # 3. Write back
    # ------------------------------------------------------------------
    claude_json_path.parent.mkdir(parents=True, exist_ok=True)
    claude_json_path.write_text(json.dumps(config, indent=2) + "\n")
    actions.append(f"Updated {claude_json_path} with mcpServers.longhouse")

    logger.info("Registered Longhouse MCP server in %s", claude_json_path)
    return actions


# ---------------------------------------------------------------------------
# TOML writing helpers (no external dependency needed)
# ---------------------------------------------------------------------------

# Regex matching the [mcp_servers.longhouse] section in TOML.
# Captures from the header through all key=value lines until the next
# section header or end-of-file.
_CODEX_MCP_SECTION_RE = re.compile(
    r"^\[mcp_servers\.longhouse\]\s*\n(?:(?!\[)[^\n]*\n?)*",
    re.MULTILINE,
)


def _toml_escape(value: str) -> str:
    """Escape a string value for safe embedding in a TOML basic string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_codex_mcp_section(api_url: str | None = None) -> str:
    """Build the TOML snippet for the Longhouse MCP server entry.

    Args:
        api_url: Optional Longhouse API URL.  When provided, the MCP
                 server args include ``--url <api_url>`` so workspace-
                 scoped Codex sessions connect to the correct instance.
    """
    if api_url:
        safe_url = _toml_escape(api_url)
        args_line = f'args = ["mcp-server", "--url", "{safe_url}"]'
    else:
        args_line = 'args = ["mcp-server"]'
    return f"[mcp_servers.longhouse]\n" f'command = "longhouse"\n' f"{args_line}\n"


def upsert_codex_mcp_toml(
    config_path: Path,
    *,
    api_url: str | None = None,
    strict: bool = True,
) -> None:
    """Add or update the ``[mcp_servers.longhouse]`` section in a Codex config.toml.

    This is the single shared implementation for both user-global
    (``~/.codex/config.toml``) and workspace-scoped
    (``.codex/config.toml``) Codex MCP registration.

    Args:
        config_path: Path to the ``config.toml`` file.
        api_url: Optional Longhouse API URL to pass via ``--url``.
        strict: If True (default), raise on corrupt TOML.  If False,
                start fresh (appropriate for workspace provisioning
                where best-effort is acceptable).
    """
    existing_text = ""
    if config_path.exists():
        existing_text = config_path.read_text(encoding="utf-8")
        if existing_text.strip():
            try:
                tomllib.loads(existing_text)
            except tomllib.TOMLDecodeError as exc:
                if strict:
                    raise RuntimeError(
                        f"Failed to parse {config_path}: {exc}. " "Fix or remove the file manually before registering MCP server."
                    ) from exc
                logger.warning("Corrupt TOML in %s, starting fresh: %s", config_path, exc)
                existing_text = ""

    new_section = _build_codex_mcp_section(api_url=api_url)

    if _CODEX_MCP_SECTION_RE.search(existing_text):
        updated_text = _CODEX_MCP_SECTION_RE.sub(new_section, existing_text)
    else:
        separator = "\n" if existing_text and not existing_text.endswith("\n") else ""
        updated_text = existing_text + separator + new_section

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(updated_text, encoding="utf-8")


def install_codex_mcp_server(codex_dir: str | None = None) -> list[str]:
    """Register the Longhouse MCP server in Codex CLI ``config.toml``.

    Codex CLI reads MCP server configuration from
    ``~/.codex/config.toml`` using ``[mcp_servers.<name>]`` sections.

    This function is idempotent — it adds or updates the
    ``[mcp_servers.longhouse]`` section while preserving all other
    configuration.

    Args:
        codex_dir: Override for Codex config directory (default:
                   ``~/.codex``). Used for testing.

    Returns:
        List of human-readable action strings describing what was done.
    """
    actions: list[str] = []

    if codex_dir:
        config_path = Path(codex_dir).expanduser() / "config.toml"
    else:
        config_path = Path.home() / ".codex" / "config.toml"

    upsert_codex_mcp_toml(config_path, strict=True)
    actions.append(f"Updated {config_path} with [mcp_servers.longhouse]")

    logger.info("Registered Longhouse MCP server in %s", config_path)
    return actions
