"""Claude Code and Codex hook installation and shared workspace MCP helpers.

Installs provider hook scripts and injects hook configuration into
``~/.claude/settings.json`` and ``~/.codex/hooks.json`` so Longhouse can
write presence events locally without network calls or SQLite calls in the hot
path. The daemon persists durable managed transcript bindings from those
events.

Claude hooks (via settings.json):

- **native ``longhouse-engine claude-lifecycle-hook``** (SessionStart, Stop,
  UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, Notification):
  writes presence events to a local outbox directory
  (``~/.longhouse/agent/outbox/``) as small JSON files (<2ms, no network or
  SQLite).

Codex hooks (via hooks.json):

- **longhouse-codex-hook.sh** (UserPromptSubmit, PreToolUse, PostToolUse,
  PermissionRequest, Stop):
  Same pattern as Claude. Codex has fewer hook events (no
  Notification hook), so idle-prompt granularity is not available there. The
  Codex bridge owns initial presence and transcript binding; avoiding
  SessionStart also avoids stock Codex's visible post-compaction hook cards.

Antigravity hooks (when Antigravity is installed):

- **longhouse-antigravity-hook.sh**: local presence/inbox boundary. Managed
  transcript binding is persisted by the daemon after the presence record is
  published.

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
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

from zerg.provider_contract.hook_scripts import CODEX_HOOK_SCRIPT
from zerg.provider_contract.hook_scripts import COORDINATION_BOOTSTRAP  # noqa: F401  (re-exported)
from zerg.provider_contract.hook_scripts import HOOK_SCRIPT  # noqa: F401  (re-exported)
from zerg.provider_contract.hook_scripts import PERMISSION_GATE_SCRIPT  # noqa: F401  (re-exported)
from zerg.provider_contract.longhouse_paths import resolve_longhouse_home_from_provider_home

logger = logging.getLogger(__name__)

# Markers used to identify generated lifecycle hooks inside settings.json so we
# can update in place rather than blindly appending duplicates. Keep this
# explicit: a user's unrelated ``longhouse-helper.sh`` is not ours to replace.
_HOOK_MARKERS = (
    "longhouse-hook.sh",
    "longhouse-session-start.sh",
    "longhouse-presence.sh",
    "longhouse-ship.sh",
    "claude-lifecycle-hook",
)


def _make_hook_entries(engine_path: str, longhouse_home: Path) -> tuple[dict, dict]:
    """Build native Claude hook entries with explicit local state.

    Returns (stop_entry, lifecycle_entry):
    - stop_entry: native engine command for Stop (sync, local outbox write)
    - lifecycle_entry: native engine command for lifecycle events
    """
    command = f"LONGHOUSE_HOME={shlex.quote(str(longhouse_home))} {shlex.quote(engine_path)} claude-lifecycle-hook"

    # Stop: sync — hook only writes the local presence outbox.
    # The daemon handles transcript shipping via its file watcher.
    stop_entry = {
        "hooks": [
            {"type": "command", "command": command, "async": False, "timeout": 5},
        ],
    }
    # Lifecycle events remain sync and local-only.
    lifecycle_entry = {
        "hooks": [
            {"type": "command", "command": command, "async": False, "timeout": 5},
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


def _merge_hook_entry_by_command(
    existing_entries: list[dict],
    new_entry: dict,
    command_substr: str | tuple[str, ...],
) -> list[dict]:
    """Upsert a hook entry identified by a specific command substring.

    Unlike _merge_hooks_for_event (which replaces lifecycle hooks), this only
    replaces entries whose command contains one of ``command_substr`` markers,
    leaving unrelated hooks on the same event untouched. Duplicate matching
    entries collapse to one.
    """
    updated = False
    result: list[dict] = []
    for entry in existing_entries:
        markers = (command_substr,) if isinstance(command_substr, str) else command_substr
        matches = any(any(marker in hook.get("command", "") for marker in markers) for hook in entry.get("hooks", []))
        if matches:
            # Replace the first matching entry; drop any further duplicates so
            # repeated/old installs converge to exactly one gate entry.
            if not updated:
                result.append(new_entry)
                updated = True
        else:
            result.append(entry)
    if not updated:
        result.append(new_entry)
    return result


def _is_longhouse_hook(entry: dict) -> bool:
    """Return True if a hook entry is the Longhouse lifecycle hook.

    Checks whether any inner hook's ``command`` field contains the marker so we
    can update it in place. Permission-gate hooks are intentionally EXCLUDED:
    they are separate entries upserted by _merge_hook_entry_by_command, so the
    lifecycle merge must not replace them.
    """
    for hook in entry.get("hooks", []):
        cmd = hook.get("command", "")
        if "longhouse-permission-gate" in cmd or "claude-permission-gate" in cmd:
            continue
        if any(marker in cmd for marker in _HOOK_MARKERS):
            return True
    return False


def _merge_hooks_for_event(
    existing_entries: list[dict],
    new_entry: dict,
) -> list[dict]:
    """Merge a Longhouse hook entry into an existing list for one event.

    If one or more Longhouse lifecycle hooks already exist in the list, they are
    replaced by exactly one new entry. Non-Longhouse hooks are left untouched.

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
            # Replace the first existing lifecycle entry and drop duplicates so
            # repeated or partially migrated installs converge to one path.
            if not updated:
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
    _write_text_if_changed(settings_path, json.dumps(data, indent=2) + "\n")


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
    """Install native Longhouse Claude hooks and provider hook integrations.

    This function is idempotent — running it multiple times updates
    existing hooks rather than creating duplicates.

    Steps performed:
    1. Create ``~/.claude/hooks/`` directory.
    2. Resolve the paired engine used for native Claude lifecycle hooks.
    3. Read ``~/.claude/settings.json`` (or start with ``{}``).
    4. Upsert Longhouse hook entries into the ``hooks`` object.
    5. Remove obsolete shell/Python hook scripts superseded by native hooks.
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
    # 2. Resolve the paired engine used by every native Claude hook command.
    # ------------------------------------------------------------------
    longhouse_home = resolve_longhouse_home_from_provider_home(config_dir)
    if engine_path is None:
        try:
            from zerg.services.shipper.service import get_engine_executable

            engine_path = get_engine_executable()
        except RuntimeError:
            engine_path = "longhouse-engine"  # last resort: rely on PATH

    # Remove generated scripts from the pre-native installer. The settings
    # entries below become the single Claude lifecycle and permission paths.
    for deprecated in (
        "longhouse-hook.sh",
        "longhouse-permission-gate.py",
        "longhouse-ship.sh",
        "longhouse-presence.sh",
        "longhouse-session-start.sh",
    ):
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
    stop_entry, lifecycle_entry = _make_hook_entries(engine_path, longhouse_home)
    hooks_obj = settings.setdefault("hooks", {})

    # Stop: async (ship is long-running; sync Stop hooks always show "hook feedback" in Claude)
    stop_list = hooks_obj.get("Stop", [])
    hooks_obj["Stop"] = _merge_hooks_for_event(stop_list, stop_entry)

    # Lifecycle events: sync, local-only native engine command.
    for event in (
        "SessionStart",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionRequest",
        "Notification",
        "CwdChanged",
    ):
        raw = hooks_obj.get(event, [])
        event_list = raw if isinstance(raw, list) else []
        hooks_obj[event] = _merge_hooks_for_event(event_list, lifecycle_entry)

    # PreToolUse additionally carries the native permission-gate command as its
    # own entry. It is dormant unless the launcher enables remote approval.
    permission_gate_entry = {
        "hooks": [
            {
                "type": "command",
                "command": (f"LONGHOUSE_HOME={shlex.quote(str(longhouse_home))} {shlex.quote(engine_path)} claude-permission-gate"),
                "async": False,
                "timeout": 30,
            }
        ],
    }
    pre_list = hooks_obj.get("PreToolUse", [])
    hooks_obj["PreToolUse"] = _merge_hook_entry_by_command(
        pre_list,
        permission_gate_entry,
        ("longhouse-permission-gate.py", "claude-permission-gate"),
    )

    # ------------------------------------------------------------------
    # 5. Write settings back
    # ------------------------------------------------------------------
    _write_settings(settings_path, settings)
    actions.append(
        f"Updated {settings_path} with SessionStart, SessionEnd, Stop, SubagentStart, "
        "SubagentStop, TaskCreated, TaskCompleted, UserPromptSubmit, PreToolUse, "
        "PostToolUse, PostToolUseFailure, PermissionRequest, Notification, and CwdChanged hooks"
    )

    logger.info("Installed Longhouse hooks in %s", config_dir)

    # ------------------------------------------------------------------
    # 6. Install Codex hooks (best-effort — Codex may not be installed)
    # ------------------------------------------------------------------
    codex_actions = install_codex_hooks(engine_path=engine_path, claude_dir=claude_dir)
    actions.extend(codex_actions)

    from zerg.services.cursor_hooks import install_cursor_hooks

    actions.extend(install_cursor_hooks(engine_path=engine_path))

    # Keep the third-party Antigravity plugin in the same repair/install loop
    # as Claude, Codex, and Cursor. `.gemini` is also owned by Gemini CLI, so
    # require the Antigravity binary or its provider-specific config root.
    antigravity_bin = shutil.which("agy")
    antigravity_cli_root = Path.home() / ".gemini" / "antigravity-cli"
    if antigravity_bin or antigravity_cli_root.exists():
        from zerg.provider_contract.antigravity_hook_inbox import _ensure_antigravity_runtime_plugin

        plugin_root = _ensure_antigravity_runtime_plugin(antigravity_bin=antigravity_bin)
        actions.append(f"Configured Antigravity hooks in {plugin_root}")

    return actions


# ---------------------------------------------------------------------------
# Codex hooks.json management
# ---------------------------------------------------------------------------

_CODEX_HOOK_MARKER = "longhouse-codex-hook.sh"


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
    3. Remove the obsolete Longhouse SessionStart entry and upsert the
       remaining lifecycle hooks
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

    # Codex re-runs SessionStart hooks after every compaction and defers their
    # execution until the next turn. Remove only Longhouse's entry so several
    # compactions cannot render a batch of identical hook cards. User hooks are
    # preserved. Durable coordination awareness now comes from Longhouse MCP
    # server instructions instead.
    existing_session_start = hooks_obj.get("SessionStart", [])
    if isinstance(existing_session_start, list):
        remaining_session_start = [group for group in existing_session_start if not _is_longhouse_codex_hook(group)]
        if remaining_session_start:
            hooks_obj["SessionStart"] = remaining_session_start
        else:
            hooks_obj.pop("SessionStart", None)

    existing_stop = hooks_obj.get("Stop", [])
    if not isinstance(existing_stop, list):
        existing_stop = []
    hooks_obj["Stop"] = _merge_codex_hooks_for_event(existing_stop, stop_group)

    # ------------------------------------------------------------------
    # 4. Write hooks.json back
    # ------------------------------------------------------------------
    hooks_json_content = json.dumps(hooks_data, indent=2) + "\n"
    if _write_text_if_changed(hooks_json_path, hooks_json_content):
        actions.append(f"Updated {hooks_json_path} with UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, Stop hooks")
    else:
        actions.append(f"{hooks_json_path} already up to date")

    logger.info("Installed Longhouse Codex hooks in %s", codex_dir)
    return actions


def _write_text_if_changed(path: Path, content: str, *, mode: int | None = None) -> bool:
    try:
        if path.exists() and path.read_text() == content:
            if mode is not None:
                path.chmod(mode)
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is None:
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except OSError:
                mode = stat.S_IRUSR | stat.S_IWUSR
        temporary.chmod(mode)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return True


def _chmod_if_needed(path: Path, mode: int) -> bool:
    try:
        if stat.S_IMODE(path.stat().st_mode) == mode:
            return False
    except OSError:
        pass
    path.chmod(mode)
    return True
