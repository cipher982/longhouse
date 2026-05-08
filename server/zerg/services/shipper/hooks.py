"""Claude Code and Codex hook installation and shared workspace MCP helpers.

Installs provider hook scripts and injects hook configuration into
``~/.claude/settings.json`` and ``~/.codex/hooks.json`` so Longhouse can
write presence and binding events locally without network calls in the hot
path.

Claude hooks (via settings.json):

- **longhouse-hook.sh** (SessionStart, Stop, UserPromptSubmit, PreToolUse,
  PostToolUse, PermissionRequest, Notification):
  Writes presence events to a local outbox directory
  (``~/.longhouse/agent/outbox/``) as small JSON files (<2ms, no network) and
  seeds session binding for the daemon.

Codex hooks (via hooks.json):

- **longhouse-codex-hook.sh** (SessionStart, UserPromptSubmit, PreToolUse,
  PostToolUse, PermissionRequest, Stop):
  Same pattern as Claude. Codex has fewer hook events (no
  Notification hook), so idle-prompt granularity is not available there.

Startup continuity injection (fetching recent project context on
SessionStart) is not part of the default hook. See
``labs/startup-continuity/`` for the opt-in installer that adds it.

Usage:
    from zerg.services.shipper.hooks import install_hooks

    actions = install_hooks(url="https://api.longhouse.ai")
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

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hook script templates
# ---------------------------------------------------------------------------

HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse unified Claude hook — presence outbox + session binding seed
# Installed by: longhouse connect --install
# Registered on: SessionStart, Stop, UserPromptSubmit, PreToolUse,
#                PostToolUse, PostToolUseFailure, PermissionRequest, Notification
# All events: local-only presence outbox write + session binding seed.
INPUT=$(cat)
LONGHOUSE_HOME="${LONGHOUSE_HOME:-__LONGHOUSE_HOME__}"

# Require jq — exit silently if missing (hook is best-effort)
command -v jq >/dev/null 2>&1 || exit 0

# Parse all fields in a single jq call using unit-separator (\\x1f) as delimiter.
# @tsv would split on spaces inside field values; \\x1f is safe for paths/tool names.
IFS=$'\\x1f' read -r EVENT SESSION_ID TOOL CWD TRANSCRIPT NOTIF_TYPE <<< "$(
  printf '%s' "$INPUT" | jq -r '[
    (.hook_event_name // ""),
    (.session_id // ""),
    (.tool_name // ""),
    (.cwd // ""),
    (.transcript_path // ""),
    (.notification_type // "")
  ] | join("\\u001f")'
)"

MANAGED_SESSION_ID="${LONGHOUSE_MANAGED_SESSION_ID:-}"
[ -n "$MANAGED_SESSION_ID" ] && SESSION_ID="$MANAGED_SESSION_ID"

FORCE_SIDECHAIN="${LONGHOUSE_IS_SIDECHAIN:-0}"
HINDSIGHT_ROOT="__HINDSIGHT_ROOT__"
if [[ "$FORCE_SIDECHAIN" != "1" ]] && [[ -n "$CWD" ]]; then
  case "$CWD" in
    "$HINDSIGHT_ROOT"|"$HINDSIGHT_ROOT"/*) FORCE_SIDECHAIN="1" ;;
  esac
fi

write_presence_outbox() {
  payload="$1"
  OUTBOX="$LONGHOUSE_HOME/agent/outbox"
  [ -d "$OUTBOX" ] || mkdir -p "$OUTBOX" || return 1
  TMPFILE=$(mktemp "$OUTBOX/.tmp.XXXXXX") || return 1
  printf '%s\n' "$payload" > "$TMPFILE" || { rm -f "$TMPFILE"; return 1; }
  mv "$TMPFILE" "${TMPFILE/\\.tmp\\./prs.}.json"
}

find_provider_pid() {
  pid="$$"
  while [[ -n "$pid" && "$pid" != "0" ]]; do
    comm="$(ps -p "$pid" -o comm= 2>/dev/null | awk '{print $1}')"
    base="${comm##*/}"
    if [[ "$base" == "claude" ]]; then
      printf '%s' "$pid"
      return 0
    fi
    pid="$(ps -p "$pid" -o ppid= 2>/dev/null | awk '{print $1}')"
  done
  return 1
}

# Map event → presence state
case "$EVENT" in
  SessionStart)                    STATE="idle" ;;
  UserPromptSubmit)               STATE="thinking" ;;
  PreToolUse)                     STATE="running" ;;
  PostToolUse|PostToolUseFailure) STATE="thinking" ;;
  Stop)                           STATE="idle" ;;
  PermissionRequest)              STATE="blocked" ;;
  Notification)
    case "$NOTIF_TYPE" in
      idle_prompt|elicitation_dialog) STATE="needs_user" ;;
      permission_prompt)              STATE="blocked" ;;
      *)                              STATE="" ;;
    esac
    ;;
  *)                              STATE="" ;;
esac

if [[ -n "$STATE" ]] && [[ -n "$SESSION_ID" ]]; then
  CONTROL_PATH="unmanaged"
  PROVIDER_PID=""
  if [[ -n "$MANAGED_SESSION_ID" ]]; then
    CONTROL_PATH="managed"
  else
    PROVIDER_PID="$(find_provider_pid || true)"
  fi

  PAYLOAD=$(jq -n --arg sid "$SESSION_ID" --arg st "$STATE" \\
        --arg tool "$TOOL" --arg cwd "$CWD" --arg transcript "$TRANSCRIPT" \\
        --arg provider "claude" --arg control_path "$CONTROL_PATH" \\
        --arg provider_pid "$PROVIDER_PID" \\
    '{session_id: $sid, state: $st, tool_name: $tool, cwd: $cwd, provider: $provider, transcript_path: $transcript, control_path: $control_path}
      + (if $provider_pid == "" then {} else {provider_pid: ($provider_pid | tonumber)} end)')

  # Seed session binding so the daemon ships with the correct managed session ID.
  # The daemon (longhouse-engine connect) handles all transcript shipping via its
  # file watcher — hooks no longer ship directly.
  ENGINE="__ENGINE_PATH__"
  if [[ -n "$MANAGED_SESSION_ID" ]] && [[ -n "$TRANSCRIPT" ]]; then
    "$ENGINE" bind --path "$TRANSCRIPT" --session-id "$MANAGED_SESSION_ID" >/dev/null 2>&1 || true
  fi

  write_presence_outbox "$PAYLOAD" >/dev/null 2>&1 || true
fi

# Always exit 0 — hook errors trigger Claude Code's "What should Claude do
# instead?" prompt, which interrupts the session.
exit 0
"""

# ---------------------------------------------------------------------------
# Codex hook script template
# ---------------------------------------------------------------------------

CODEX_HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse Codex hook — presence outbox + session binding seed
# Installed by: longhouse connect --install
# Registered on: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
#                PermissionRequest, Stop (via ~/.codex/hooks.json)
# All events: local-only presence outbox write + session binding seed.
INPUT=$(cat)
LONGHOUSE_HOME="${LONGHOUSE_HOME:-__LONGHOUSE_HOME__}"

# Codex command hooks use snake_case field names.
IFS=$'\\x1f' read -r EVENT CODEX_SESSION_ID TOOL CWD TRANSCRIPT <<< "$(
  printf '%s' "$INPUT" | jq -r '[
    (.hook_event_name // ""),
    (.session_id // ""),
    (.tool_name // ""),
    (.cwd // ""),
    (.transcript_path // "")
  ] | join("\\u001f")'
)"

# Session ID resolution — managed sessions use the launcher-injected env.
MANAGED_SESSION_ID="${LONGHOUSE_MANAGED_SESSION_ID:-}"
if [ -n "$MANAGED_SESSION_ID" ]; then
  SID="$MANAGED_SESSION_ID"
else
  SID="$CODEX_SESSION_ID"
fi

write_presence_outbox() {
  payload="$1"
  OUTBOX="$LONGHOUSE_HOME/agent/outbox"
  [ -d "$OUTBOX" ] || mkdir -p "$OUTBOX" || return 1
  TMPFILE=$(mktemp "$OUTBOX/.tmp.XXXXXX") || return 1
  printf '%s\n' "$payload" > "$TMPFILE" || { rm -f "$TMPFILE"; return 1; }
  mv "$TMPFILE" "${TMPFILE/\\.tmp\\./prs.}.json"
}

# Map event -> presence state
case "$EVENT" in
  SessionStart)         STATE="idle" ;;
  UserPromptSubmit)     STATE="thinking" ;;
  PreToolUse)           STATE="running" ;;
  # Codex exposes PostToolUse, but not Claude's PostToolUseFailure event.
  PostToolUse)          STATE="thinking" ;;
  PermissionRequest)    STATE="blocked" ;;
  Stop)                 STATE="idle" ;;
  *)                    STATE="" ;;
esac

if [[ -n "$STATE" ]] && [[ -n "$SID" ]]; then
  PAYLOAD=$(jq -n --arg sid "$SID" --arg st "$STATE" \\
        --arg tool "$TOOL" --arg cwd "$CWD" --arg provider "codex" \\
        --arg transcript "$TRANSCRIPT" \\
    '{session_id: $sid, state: $st, tool_name: $tool, cwd: $cwd, provider: $provider, transcript_path: $transcript}')
  write_presence_outbox "$PAYLOAD" >/dev/null 2>&1 || true

  # Seed session binding so the daemon ships with the correct managed session ID.
  ENGINE="__ENGINE_PATH__"
  if [[ -n "$MANAGED_SESSION_ID" ]] && [[ -n "$TRANSCRIPT" ]]; then
    "$ENGINE" bind --path "$TRANSCRIPT" --session-id "$MANAGED_SESSION_ID" --provider codex >/dev/null 2>&1 || true
  fi
fi

exit 0
"""

# Marker used to identify Longhouse hooks inside settings.json so we can
# update in place rather than blindly appending duplicates.  Use the path
# prefix "longhouse-" which is specific enough to avoid false positives on
# user hooks that happen to mention "longhouse" in a description.
_HOOK_MARKER = "longhouse-"


def _make_hook_entries(hooks_dir: Path) -> tuple[dict, dict]:
    """Build hook entry dicts with resolved script paths.

    Returns (stop_entry, lifecycle_entry):
    - stop_entry: unified script for Stop (sync, local write/bind — not a banner)
    - lifecycle_entry: unified script for SessionStart and other lifecycle hooks
    """
    hook_path = str(hooks_dir / "longhouse-hook.sh")

    # Stop: sync — hook does local session binding and presence delivery setup.
    # The daemon handles transcript shipping via its file watcher.
    stop_entry = {
        "hooks": [
            {"type": "command", "command": hook_path, "async": False, "timeout": 5},
        ],
    }
    # Lifecycle events remain sync and local-only.
    lifecycle_entry = {
        "hooks": [
            {"type": "command", "command": hook_path, "async": False, "timeout": 5},
        ],
    }
    return stop_entry, lifecycle_entry


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
        message = f"Failed to parse {settings_path}: {exc}. "
        message += "Fix or remove the file manually before installing hooks."
        raise RuntimeError(message) from exc


def _write_settings(settings_path: Path, data: dict) -> None:
    """Write settings dict back to settings.json with indent=2."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def _shell_double_quote(value: str) -> str:
    """Escape a string for safe insertion into a shell double-quoted context."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


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
    2. Write ``longhouse-hook.sh`` with executable permissions.
    3. Read ``~/.claude/settings.json`` (or start with ``{}``).
    4. Upsert Longhouse hook entries into the ``hooks`` object.
    5. Remove deprecated standalone SessionStart scripts superseded by the
       unified hook.
    6. Write ``settings.json`` back.

    Args:
        url: Longhouse API URL (used for logging only; hot-path presence writes
             go through the local outbox, not the network).
        token: Unused legacy arg retained for compatibility.
        claude_dir: Override for Claude config directory.

    Returns:
        List of human-readable action strings describing what was done.
    """
    config_dir = _resolve_claude_dir(claude_dir)
    hooks_dir = config_dir / "hooks"
    projects_dir = config_dir / "projects"
    settings_path = config_dir / "settings.json"
    actions: list[str] = []

    # ------------------------------------------------------------------
    # 1. Create config directories the engine expects on first install.
    # ------------------------------------------------------------------
    hooks_dir.mkdir(parents=True, exist_ok=True)
    projects_dir.mkdir(parents=True, exist_ok=True)
    actions.append(f"Ensured {projects_dir}")

    # ------------------------------------------------------------------
    # 2. Write hook scripts with explicit provider + Longhouse paths baked in.
    # ------------------------------------------------------------------
    longhouse_home = resolve_longhouse_home_from_provider_home(config_dir)
    hindsight_root = config_dir / "hindsight"

    # Resolve engine path at install time and bake it into the hook script.
    if engine_path is None:
        try:
            from zerg.services.shipper.service import get_engine_executable

            engine_path = get_engine_executable()
        except RuntimeError:
            engine_path = "longhouse-engine"  # last resort: rely on PATH

    hook_script_content = (
        HOOK_SCRIPT.replace(
            "__LONGHOUSE_HOME__",
            _shell_double_quote(str(longhouse_home)),
        )
        .replace(
            "__HINDSIGHT_ROOT__",
            _shell_double_quote(str(hindsight_root)),
        )
        .replace(
            "__ENGINE_PATH__",
            engine_path,
        )
    )
    hook_script = hooks_dir / "longhouse-hook.sh"
    hook_script.write_text(hook_script_content)
    hook_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    actions.append(f"Wrote {hook_script}")

    # Remove deprecated standalone hook scripts (superseded by longhouse-hook.sh).
    for deprecated in ("longhouse-ship.sh", "longhouse-presence.sh", "longhouse-session-start.sh"):
        deprecated_path = hooks_dir / deprecated
        if deprecated_path.exists():
            deprecated_path.unlink()
            actions.append(f"Removed deprecated {deprecated_path}")

    # ------------------------------------------------------------------
    # 3. Read existing settings
    # ------------------------------------------------------------------
    settings = _read_settings(settings_path)

    # ------------------------------------------------------------------
    # 4. Merge hook entries (using resolved absolute paths)
    # ------------------------------------------------------------------
    stop_entry, lifecycle_entry = _make_hook_entries(hooks_dir)
    hooks_obj = settings.setdefault("hooks", {})

    # Stop: async (ship is long-running; sync Stop hooks always show "hook feedback" in Claude)
    stop_list = hooks_obj.get("Stop", [])
    hooks_obj["Stop"] = _merge_hooks_for_event(stop_list, stop_entry)

    # Lifecycle events: sync, local-only (outbox write <2ms).
    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionRequest",
        "Notification",
    ):
        raw = hooks_obj.get(event, [])
        event_list = raw if isinstance(raw, list) else []
        hooks_obj[event] = _merge_hooks_for_event(event_list, lifecycle_entry)

    # ------------------------------------------------------------------
    # 5. Write settings back
    # ------------------------------------------------------------------
    _write_settings(settings_path, settings)
    actions.append(
        f"Updated {settings_path} with SessionStart, Stop, UserPromptSubmit, PreToolUse, "
        "PostToolUse, PermissionRequest, and Notification hooks"
    )

    logger.info("Installed Longhouse hooks in %s", config_dir)

    # ------------------------------------------------------------------
    # 6. Install Codex hooks (best-effort — Codex may not be installed)
    # ------------------------------------------------------------------
    codex_actions = install_codex_hooks(engine_path=engine_path, claude_dir=claude_dir)
    actions.extend(codex_actions)

    return actions


# ---------------------------------------------------------------------------
# Codex hooks.json management
# ---------------------------------------------------------------------------

_CODEX_HOOK_MARKER = "longhouse-"


def _resolve_codex_dir() -> Path:
    """Resolve the Codex config directory (~/.codex)."""
    return Path.home() / ".codex"


def _is_longhouse_codex_hook(entry: dict) -> bool:
    """Return True if a Codex hooks.json MatcherGroup belongs to Longhouse."""
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "")
        if _CODEX_HOOK_MARKER in cmd:
            return True
    return False


def _merge_codex_hooks_for_event(
    existing_groups: list[dict],
    new_group: dict,
) -> list[dict]:
    """Merge a Longhouse hook into a Codex event's MatcherGroup array."""
    updated = False
    result: list[dict] = []
    for group in existing_groups:
        if _is_longhouse_codex_hook(group):
            result.append(new_group)
            updated = True
        else:
            result.append(group)
    if not updated:
        result.append(new_group)
    return result


def install_codex_hooks(
    engine_path: str | None = None,
    claude_dir: str | None = None,
) -> list[str]:
    """Install Longhouse hook script and hooks.json for Codex CLI.

    Best-effort: returns empty list if Codex is not installed (~/.codex/
    does not exist). Does not create ~/.codex/ from scratch.

    Steps:
    1. Write longhouse-codex-hook.sh to ~/.codex/hooks/
    2. Read or create ~/.codex/hooks.json
    3. Upsert Longhouse hook entries for SessionStart, UserPromptSubmit, Stop
    4. Write hooks.json back

    Returns:
        List of human-readable action strings.
    """
    codex_dir = _resolve_codex_dir()
    if not codex_dir.exists():
        return []

    actions: list[str] = []

    # ------------------------------------------------------------------
    # 1. Write Codex hook script
    # ------------------------------------------------------------------
    hooks_dir = codex_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    if engine_path is None:
        try:
            from zerg.services.shipper.service import get_engine_executable

            engine_path = get_engine_executable()
        except RuntimeError:
            engine_path = "longhouse-engine"

    longhouse_home = resolve_longhouse_home_from_provider_home(_resolve_claude_dir(claude_dir))
    hook_content = CODEX_HOOK_SCRIPT.replace(
        "__LONGHOUSE_HOME__",
        _shell_double_quote(str(longhouse_home)),
    ).replace(
        "__ENGINE_PATH__",
        engine_path,
    )

    hook_script = hooks_dir / "longhouse-codex-hook.sh"
    hook_script_changed = _write_text_if_changed(hook_script, hook_content)
    mode_changed = _chmod_if_needed(
        hook_script,
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
    )
    if hook_script_changed:
        actions.append(f"Wrote {hook_script}")
    elif mode_changed:
        actions.append(f"Updated mode for {hook_script}")
    else:
        actions.append(f"{hook_script} already up to date")

    # ------------------------------------------------------------------
    # 2. Read existing hooks.json
    # ------------------------------------------------------------------
    hooks_json_path = codex_dir / "hooks.json"
    hooks_data: dict = {}
    if hooks_json_path.exists():
        text = hooks_json_path.read_text()
        if text.strip():
            try:
                hooks_data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("Corrupt hooks.json at %s, starting fresh", hooks_json_path)
                hooks_data = {}

    # ------------------------------------------------------------------
    # 3. Merge hook entries (Codex uses PascalCase event keys)
    # ------------------------------------------------------------------
    hook_path = str(hook_script)
    hooks_obj = hooks_data.setdefault("hooks", {})

    # Codex MatcherGroup format: {hooks: [{type: "command", command: "...", timeout: N}]}
    lifecycle_group = {
        "hooks": [{"type": "command", "command": hook_path, "timeout": 5}],
    }
    stop_group = {
        "hooks": [{"type": "command", "command": hook_path, "timeout": 5}],
    }

    lifecycle_events = (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        # Codex exposes PermissionRequest, but has no Notification hook for
        # idle-prompt style attention states.
        "PermissionRequest",
    )
    for event in lifecycle_events:
        existing = hooks_obj.get(event, [])
        if not isinstance(existing, list):
            existing = []
        hooks_obj[event] = _merge_codex_hooks_for_event(existing, lifecycle_group)

    existing_stop = hooks_obj.get("Stop", [])
    if not isinstance(existing_stop, list):
        existing_stop = []
    hooks_obj["Stop"] = _merge_codex_hooks_for_event(existing_stop, stop_group)

    # ------------------------------------------------------------------
    # 4. Write hooks.json back
    # ------------------------------------------------------------------
    hooks_json_content = json.dumps(hooks_data, indent=2) + "\n"
    if _write_text_if_changed(hooks_json_path, hooks_json_content):
        actions.append(
            f"Updated {hooks_json_path} with SessionStart, UserPromptSubmit, PreToolUse, " "PostToolUse, PermissionRequest, Stop hooks"
        )
    else:
        actions.append(f"{hooks_json_path} already up to date")

    logger.info("Installed Longhouse Codex hooks in %s", codex_dir)
    return actions


def _write_text_if_changed(path: Path, content: str) -> bool:
    try:
        if path.exists() and path.read_text() == content:
            return False
    except OSError:
        pass
    path.write_text(content)
    return True


def _chmod_if_needed(path: Path, mode: int) -> bool:
    try:
        if stat.S_IMODE(path.stat().st_mode) == mode:
            return False
    except OSError:
        pass
    path.chmod(mode)
    return True


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
    return f'[mcp_servers.longhouse]\ncommand = "longhouse"\n{args_line}\n'


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
                    message = f"Failed to parse {config_path}: {exc}. "
                    message += "Fix or remove the file manually before registering MCP server."
                    raise RuntimeError(message) from exc
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
