"""Claude Code and Codex hook installation and shared workspace MCP helpers for Longhouse.

Installs hook scripts and injects hook configuration into
~/.claude/settings.json and ~/.codex/hooks.json so that both Claude Code
and Codex automatically ship sessions and report real-time presence
without network calls in the hook hot path.

Claude hooks (via settings.json):

- **longhouse-hook.sh** (Stop, UserPromptSubmit, PreToolUse, PostToolUse):
  Unified hook. Writes presence events to a local outbox directory
  (~/.claude/outbox/) as small JSON files (<2ms, no network). The
  longhouse-engine daemon drains the outbox on a 1-second poll and POSTs
  to /api/agents/presence. On Stop, also seeds the session_binding table
  so the daemon ships with the correct managed session ID. All
  registrations use async: False — no banners.
- **longhouse-session-start.sh** (SessionStart): On fresh session startup,
  queries Longhouse for recent sessions in the current project and injects
  a system message with context. Runs once per session (sync is acceptable).

Codex hooks (via hooks.json):

- **longhouse-codex-hook.sh** (SessionStart, UserPromptSubmit, Stop):
  Writes presence events to the same ~/.claude/outbox/ directory using
  Codex's snake_case hook command input fields. Seeds session_binding for
  managed sessions so the daemon ships with the correct ID.
  Codex has fewer hook events than Claude (no PreToolUse/PostToolUse),
  so tool-level granularity is not available.

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hook script templates
# ---------------------------------------------------------------------------

HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse unified hook — presence outbox write + session binding
# Installed by: longhouse connect --install
# Registered on: Stop, UserPromptSubmit, PreToolUse, PostToolUse,
#                PermissionRequest, Notification
# Presence: no network — writes to local outbox, daemon handles upload.
# Session binding: seeds managed session ID so daemon ships correctly.
INPUT=$(cat)

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
[ -z "$SESSION_ID" ] && exit 0

TARGET_URL="${LONGHOUSE_HOOK_URL:-}"
TARGET_TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"
FORCE_SIDECHAIN="${LONGHOUSE_IS_SIDECHAIN:-0}"
HINDSIGHT_ROOT="$HOME/.claude/hindsight"
if [[ "$FORCE_SIDECHAIN" != "1" ]] && [[ -n "$CWD" ]]; then
  case "$CWD" in
    "$HINDSIGHT_ROOT"|"$HINDSIGHT_ROOT"/*) FORCE_SIDECHAIN="1" ;;
  esac
fi

emit_presence() {
  payload="$1"
  if [[ -n "$TARGET_URL" ]] && [[ -n "$TARGET_TOKEN" ]] && command -v curl >/dev/null 2>&1; then
    printf '%s' "$payload" | curl -sf --connect-timeout 0.5 --max-time 1.5 \\
      -H "Content-Type: application/json" \\
      -H "X-Agents-Token: $TARGET_TOKEN" \\
      --data-binary @- \\
      "${TARGET_URL%/}/api/agents/presence" >/dev/null 2>&1 && return 0
  fi
  return 1
}

# Map event → presence state
case "$EVENT" in
  UserPromptSubmit)               STATE="thinking" ;;
  PreToolUse)                     STATE="running" ;;
  PostToolUse|PostToolUseFailure) STATE="thinking" ;;
  Stop)                           STATE="idle" ;;
  PermissionRequest)              STATE="blocked" ;;
  Notification)
    case "$NOTIF_TYPE" in
      idle_prompt|elicitation_dialog) STATE="needs_user" ;;
      permission_prompt)              STATE="blocked" ;;
      *)                              exit 0 ;;
    esac
    ;;
  *)                              exit 0 ;;
esac

PAYLOAD=$(jq -n --arg sid "$SESSION_ID" --arg st "$STATE" \\
      --arg tool "$TOOL" --arg cwd "$CWD" \\
  '{session_id: $sid, state: $st, tool_name: $tool, cwd: $cwd}')

# Seed session binding so the daemon ships with the correct managed session ID.
# The daemon (longhouse-engine connect) handles all transcript shipping via its
# file watcher — hooks no longer ship directly.
ENGINE="__ENGINE_PATH__"
if [[ -n "$MANAGED_SESSION_ID" ]] && [[ -n "$TRANSCRIPT" ]]; then
  "$ENGINE" bind --path "$TRANSCRIPT" --session-id "$MANAGED_SESSION_ID" >/dev/null 2>&1 || true
fi

if ! emit_presence "$PAYLOAD"; then
  # Write presence to outbox (atomic: write to .tmp.* then rename to prs.*.json)
  # Temp file starts with '.' so the daemon skips it during the write.
  # Final file starts with 'prs.' — daemon picks it up, POSTs, deletes.
  OUTBOX="$HOME/.claude/outbox"
  [ -d "$OUTBOX" ] || mkdir -p "$OUTBOX"
  TMPFILE=$(mktemp "$OUTBOX/.tmp.XXXXXX")
  printf '%s\n' "$PAYLOAD" > "$TMPFILE"
  mv "$TMPFILE" "${TMPFILE/\\.tmp\\./prs.}.json"
fi

# Always exit 0 — hook errors trigger Claude Code's "What should Claude do
# instead?" prompt, which interrupts the session.
exit 0
"""

SESSION_START_HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse SessionStart hook — injects recent session summaries on startup
# Installed by: longhouse connect --install
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
SOURCE=$(echo "$INPUT" | jq -r '.source // empty')

# Only fire on fresh session start (not resume/compact)
if [[ "$SOURCE" != "startup" ]]; then exit 0; fi

PROJECT=$(basename "$CWD")
if [[ -z "$PROJECT" ]]; then exit 0; fi

TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"
URL="${LONGHOUSE_HOOK_URL:-}"
if [[ -z "$TOKEN" ]] || [[ -z "$URL" ]]; then
  TOKEN_FILE="$HOME/.claude/longhouse-device-token"
  URL_FILE="$HOME/.claude/longhouse-url"
  if [[ ! -f "$TOKEN_FILE" ]] || [[ ! -f "$URL_FILE" ]]; then exit 0; fi
  TOKEN=$(cat "$TOKEN_FILE" | tr -d '[:space:]')
  URL=$(cat "$URL_FILE" | tr -d '[:space:]')
fi

# URL-encode the project name so paths with spaces or special chars work.
PROJECT_ENC=$(
  python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$PROJECT" 2>/dev/null \\
    || printf '%s' "$PROJECT"
)

RESPONSE=$(curl -sf --max-time 5 \\
  -H "X-Agents-Token: $TOKEN" \\
  "${URL}/api/agents/sessions?project=${PROJECT_ENC}&limit=30&days_back=14&hide_autonomous=true" 2>/dev/null)
if [[ $? -ne 0 ]] || [[ -z "$RESPONSE" ]]; then exit 0; fi

TOTAL=$(echo "$RESPONSE" | jq -r '.total // 0')
if [[ "$TOTAL" -eq 0 ]]; then exit 0; fi

# Build lines: metadata + summary for sessions that have a summary.
# Each session renders as:
#   [DATE] PROVIDER Nt — TITLE
#     SUMMARY (truncated to 200 chars)
LINES=$(echo "$RESPONSE" | python3 -c "
import json, sys, textwrap

# strict=False handles embedded control characters that can appear in session summaries
data = json.loads(sys.stdin.read(), strict=False)
out = []
for s in data.get('sessions', []):
    summary = (s.get('summary') or '').strip()
    if not summary:
        continue
    date = (s.get('started_at') or '')[:10]
    provider = s.get('provider') or '?'
    tools = s.get('tool_calls') or 0
    title = (s.get('summary_title') or '').strip()
    # Truncate summary to keep total size reasonable
    summary_short = summary[:200] + ('...' if len(summary) > 200 else '')
    header = f'[{date}] {provider} {tools}t'
    if title:
        header += f' — {title}'
    # Wrap summary at 100 chars, indent 2 spaces
    wrapped = textwrap.fill(summary_short, width=100, initial_indent='  ', subsequent_indent='  ')
    out.append(header)
    out.append(wrapped)
    if len(out) >= 60:  # ~20 sessions * 3 lines each
        break

print('\n'.join(out))
" 2>/dev/null)

if [[ -z "$LINES" ]]; then exit 0; fi

SHOWN=$(echo "$LINES" | grep -c '^\\[' 2>/dev/null || echo "?")
MSG="Longhouse: ${TOTAL} sessions in ${PROJECT} (14d) · ${SHOWN} with summaries:\\n\\n${LINES}"

jq -nc --arg msg "$MSG" '{"systemMessage": $msg}'
exit 0
"""

# ---------------------------------------------------------------------------
# Codex hook script template
# ---------------------------------------------------------------------------

CODEX_HOOK_SCRIPT = """\
#!/bin/bash
# Longhouse Codex hook — presence outbox write + session binding
# Installed by: longhouse connect --install
# Registered on: SessionStart, UserPromptSubmit, Stop (via ~/.codex/hooks.json)
# Presence: no network — writes to local outbox, daemon handles upload.
# Session binding: seeds managed session ID so daemon ships correctly.
INPUT=$(cat)

# Codex command hooks use snake_case field names.
IFS=$'\\x1f' read -r EVENT CODEX_SESSION_ID CWD TRANSCRIPT <<< "$(
  printf '%s' "$INPUT" | jq -r '[
    (.hook_event_name // ""),
    (.session_id // ""),
    (.cwd // ""),
    (.transcript_path // "")
  ] | join("\\u001f")'
)"

# Session ID resolution — managed sessions use the launcher-injected env.
MANAGED_SESSION_ID="${LONGHOUSE_MANAGED_SESSION_ID:-}"
if [ -n "$MANAGED_SESSION_ID" ]; then
  SID="$MANAGED_SESSION_ID"
else
  [ -z "$CODEX_SESSION_ID" ] && exit 0
  SID="$CODEX_SESSION_ID"
fi

TARGET_URL="${LONGHOUSE_HOOK_URL:-}"
TARGET_TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"

emit_presence() {
  payload="$1"
  if [[ -n "$TARGET_URL" ]] && [[ -n "$TARGET_TOKEN" ]] && command -v curl >/dev/null 2>&1; then
    printf '%s' "$payload" | curl -sf --connect-timeout 0.5 --max-time 1.5 \\
      -H "Content-Type: application/json" \\
      -H "X-Agents-Token: $TARGET_TOKEN" \\
      --data-binary @- \\
      "${TARGET_URL%/}/api/agents/presence" >/dev/null 2>&1 && return 0
  fi
  return 1
}

# Map event -> presence state
case "$EVENT" in
  SessionStart)         STATE="idle" ;;
  UserPromptSubmit)     STATE="thinking" ;;
  Stop)                 STATE="idle" ;;
  *)                    exit 0 ;;
esac

PAYLOAD=$(jq -n --arg sid "$SID" --arg st "$STATE" \\
      --arg tool "" --arg cwd "$CWD" --arg provider "codex" \\
  '{session_id: $sid, state: $st, tool_name: $tool, cwd: $cwd, provider: $provider}')

if ! emit_presence "$PAYLOAD"; then
  # Write presence to outbox (same outbox the engine daemon drains for Claude).
  # Atomic: write to .tmp.* then rename to prs.*.json
  OUTBOX="$HOME/.claude/outbox"
  [ -d "$OUTBOX" ] || mkdir -p "$OUTBOX"
  TMPFILE=$(mktemp "$OUTBOX/.tmp.XXXXXX")
  printf '%s\n' "$PAYLOAD" > "$TMPFILE"
  mv "$TMPFILE" "${TMPFILE/\\.tmp\\./prs.}.json"
fi

# Seed session binding so the daemon ships with the correct managed session ID.
ENGINE="__ENGINE_PATH__"
if [[ -n "$MANAGED_SESSION_ID" ]] && [[ -n "$TRANSCRIPT" ]]; then
  "$ENGINE" bind --path "$TRANSCRIPT" --session-id "$MANAGED_SESSION_ID" --provider codex >/dev/null 2>&1 || true
fi

exit 0
"""

# Marker used to identify Longhouse hooks inside settings.json so we can
# update in place rather than blindly appending duplicates.  Use the path
# prefix "longhouse-" which is specific enough to avoid false positives on
# user hooks that happen to mention "longhouse" in a description.
_HOOK_MARKER = "longhouse-"


def _make_hook_entries(hooks_dir: Path) -> tuple[dict, dict, dict]:
    """Build hook entry dicts with resolved script paths.

    Returns (stop_entry, lifecycle_entry, session_start_entry):
    - stop_entry: unified script for Stop (sync, collapsible note — not a banner)
    - lifecycle_entry: unified script for UserPromptSubmit/PreToolUse/PostToolUse
      (sync — outbox write is <2ms, silent)
    - session_start_entry: session-start script (sync network call, once per session)
    """
    hook_path = str(hooks_dir / "longhouse-hook.sh")
    session_start_path = str(hooks_dir / "longhouse-session-start.sh")

    # Stop: sync — hook only writes presence outbox + session binding (<5ms).
    # The daemon handles all transcript shipping via its file watcher.
    stop_entry = {
        "hooks": [
            {"type": "command", "command": hook_path, "async": False, "timeout": 5},
        ],
    }
    # Lifecycle events: outbox write is <2ms, sync is safe and silent.
    lifecycle_entry = {
        "hooks": [
            {"type": "command", "command": hook_path, "async": False, "timeout": 5},
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
    return stop_entry, lifecycle_entry, session_start_entry


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
        url: Longhouse API URL (used only for logging; the unified hook
             does not read it at runtime — presence goes via outbox).
        token: Device token (unused by this function; the session-start
               hook reads ``~/.claude/longhouse-device-token`` at runtime).
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

    # Resolve engine path at install time and bake it into the hook script.
    if engine_path is None:
        try:
            from zerg.services.shipper.service import get_engine_executable

            engine_path = get_engine_executable()
        except RuntimeError:
            engine_path = "longhouse-engine"  # last resort: rely on PATH

    hook_script_content = HOOK_SCRIPT.replace(
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

    hook_script = hooks_dir / "longhouse-hook.sh"
    hook_script.write_text(hook_script_content)
    hook_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    actions.append(f"Wrote {hook_script}")

    session_start_script = hooks_dir / "longhouse-session-start.sh"
    session_start_script.write_text(session_start_script_content)
    session_start_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    actions.append(f"Wrote {session_start_script}")

    # Remove deprecated hook scripts (superseded by longhouse-hook.sh).
    for deprecated in ("longhouse-ship.sh", "longhouse-presence.sh"):
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
    stop_entry, lifecycle_entry, session_start_entry = _make_hook_entries(hooks_dir)
    hooks_obj = settings.setdefault("hooks", {})

    # Stop: async (ship is long-running; sync Stop hooks always show "hook feedback" in Claude)
    stop_list = hooks_obj.get("Stop", [])
    hooks_obj["Stop"] = _merge_hooks_for_event(stop_list, stop_entry)

    # Lifecycle events: sync (outbox write <2ms, silent)
    for event in (
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

    # SessionStart hook (shows recent sessions — sync network call, once per session)
    session_start_list = hooks_obj.get("SessionStart", [])
    hooks_obj["SessionStart"] = _merge_hooks_for_event(session_start_list, session_start_entry)

    # ------------------------------------------------------------------
    # 5. Write settings back
    # ------------------------------------------------------------------
    _write_settings(settings_path, settings)
    actions.append(
        f"Updated {settings_path} with Stop, UserPromptSubmit, PreToolUse, "
        "PostToolUse, PermissionRequest, Notification, and SessionStart hooks"
    )

    logger.info("Installed Longhouse hooks in %s", config_dir)

    # ------------------------------------------------------------------
    # 6. Install Codex hooks (best-effort — Codex may not be installed)
    # ------------------------------------------------------------------
    codex_actions = install_codex_hooks(engine_path=engine_path)
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

    # Resolve outbox dir — Codex hook writes to the same outbox the engine
    # already drains (~/.claude/outbox/), keeping one daemon for all providers.
    claude_outbox = str(Path.home() / ".claude" / "outbox")
    hook_content = CODEX_HOOK_SCRIPT.replace(
        "$HOME/.claude/outbox",
        claude_outbox,
    ).replace(
        "__ENGINE_PATH__",
        engine_path,
    )

    hook_script = hooks_dir / "longhouse-codex-hook.sh"
    hook_script.write_text(hook_content)
    hook_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    actions.append(f"Wrote {hook_script}")

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

    for event in ("SessionStart", "UserPromptSubmit"):
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
    hooks_json_path.write_text(json.dumps(hooks_data, indent=2) + "\n")
    actions.append(f"Updated {hooks_json_path} with SessionStart, UserPromptSubmit, Stop hooks")

    logger.info("Installed Longhouse Codex hooks in %s", codex_dir)
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
