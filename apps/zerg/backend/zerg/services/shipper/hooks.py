"""Claude Code hook installation for Longhouse.

Installs hook scripts and injects hook configuration into
~/.claude/settings.json so that Claude Code automatically ships
sessions and displays recent session context.

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
import stat
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
longhouse ship --file "$TRANSCRIPT" --quiet 2>/dev/null
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


def _make_hook_entries(hooks_dir: Path) -> tuple[dict, dict]:
    """Build hook entry dicts with resolved script paths.

    Using absolute paths ensures consistency when ``--claude-dir`` overrides
    the default ``~/.claude`` location.
    """
    ship_path = str(hooks_dir / "longhouse-ship.sh")
    session_start_path = str(hooks_dir / "longhouse-session-start.sh")

    stop_entry = {
        "hooks": [
            {
                "type": "command",
                "command": ship_path,
                "async": True,
                "timeout": 30,
            }
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
    return stop_entry, session_start_entry


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
        raise RuntimeError(f"Failed to parse {settings_path}: {exc}. " "Fix or remove the file manually before installing hooks.") from exc


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
    ship_script_content = SHIP_HOOK_SCRIPT.replace(
        "$HOME/.claude/",
        f"{resolved_dir}/",
    )
    session_start_script_content = SESSION_START_HOOK_SCRIPT.replace(
        "$HOME/.claude/",
        f"{resolved_dir}/",
    )

    ship_script = hooks_dir / "longhouse-ship.sh"
    ship_script.write_text(ship_script_content)
    ship_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0o755
    actions.append(f"Wrote {ship_script}")

    session_start_script = hooks_dir / "longhouse-session-start.sh"
    session_start_script.write_text(session_start_script_content)
    session_start_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0o755
    actions.append(f"Wrote {session_start_script}")

    # ------------------------------------------------------------------
    # 3. Read existing settings
    # ------------------------------------------------------------------
    settings = _read_settings(settings_path)

    # ------------------------------------------------------------------
    # 4. Merge hook entries (using resolved absolute paths)
    # ------------------------------------------------------------------
    stop_entry, session_start_entry = _make_hook_entries(hooks_dir)
    hooks_obj = settings.setdefault("hooks", {})

    # Stop hook
    stop_list = hooks_obj.get("Stop", [])
    hooks_obj["Stop"] = _merge_hooks_for_event(stop_list, stop_entry)

    # SessionStart hook
    session_start_list = hooks_obj.get("SessionStart", [])
    hooks_obj["SessionStart"] = _merge_hooks_for_event(session_start_list, session_start_entry)

    # ------------------------------------------------------------------
    # 5. Write settings back
    # ------------------------------------------------------------------
    _write_settings(settings_path, settings)
    actions.append(f"Updated {settings_path} with Stop and SessionStart hooks")

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
