"""Dependency-light Antigravity hook inbox primitives."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.provider_contract.longhouse_paths import get_managed_local_dir
from zerg.provider_contract.longhouse_paths import resolve_longhouse_home

_ANTIGRAVITY_PLUGIN_NAME = "longhouse-runtime"
_ANTIGRAVITY_HOOK_SCRIPT_NAME = "longhouse-antigravity-hook.sh"
_MESSAGE_TTL = timedelta(minutes=5)
_MAX_MESSAGE_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


class _AntigravityLaunchError(Exception):
    """Raised when native Antigravity launch preparation fails."""


# The hook body is one file shared with the native launcher, which embeds the
# same bytes and rewrites an installed copy that has drifted from them.
_ANTIGRAVITY_HOOK_SCRIPT = (Path(__file__).with_name(_ANTIGRAVITY_HOOK_SCRIPT_NAME)).read_text(encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _antigravity_runtime_dir(config_dir: Path | None = None) -> Path:
    return get_managed_local_dir("antigravity", base_dir=config_dir)


def _antigravity_staged_plugin_root(antigravity_cli_root: Path | None = None) -> Path:
    cli_root = antigravity_cli_root or (Path.home() / ".gemini" / "antigravity-cli")
    return cli_root / "plugins" / _ANTIGRAVITY_PLUGIN_NAME


def _antigravity_plugin_source_root(config_dir: Path | None = None) -> Path:
    return _antigravity_runtime_dir(config_dir) / "plugins" / _ANTIGRAVITY_PLUGIN_NAME


def _antigravity_global_hooks_path() -> Path:
    return Path.home() / ".gemini" / "config" / "hooks.json"


def _antigravity_installed_plugin_hooks_path() -> Path:
    return Path.home() / ".gemini" / "config" / "plugins" / _ANTIGRAVITY_PLUGIN_NAME / "hooks.json"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is None:
            try:
                mode = path.stat().st_mode & 0o777
            except OSError:
                mode = 0o600
        tmp_path.chmod(mode)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove stale Antigravity file %s: %s", path, exc)


def antigravity_state_dir(config_dir: Path | None = None) -> Path:
    return _antigravity_runtime_dir(config_dir) / "sessions"


def antigravity_inbox_dir(session_id: str, config_dir: Path | None = None) -> Path:
    return _antigravity_runtime_dir(config_dir) / "inbox" / session_id


def _claimed_message(session_id: str, message_id: str, *, config_dir: Path | None = None) -> dict[str, Any] | None:
    claimed_dir = antigravity_inbox_dir(session_id, config_dir) / "claimed"
    for path in claimed_dir.glob("*.json"):
        payload = _read_json(path)
        if payload and payload.get("id") == message_id:
            return payload
    return None


def enqueue_antigravity_message(
    *,
    session_id: str,
    text: str,
    intent: str = "send",
    config_dir: Path | None = None,
) -> dict[str, Any]:
    normalized_session = str(session_id or "").strip()
    normalized_text = str(text or "")
    normalized_intent = str(intent or "send").strip() or "send"
    if not normalized_session:
        raise ValueError("session_id is required")
    if not normalized_text.strip():
        raise ValueError("text is required")
    if normalized_intent != "send":
        raise ValueError("Antigravity hook inbox only supports intent=send")
    if len(normalized_text.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise ValueError(f"text exceeds {_MAX_MESSAGE_BYTES} bytes")
    message_id = uuid4().hex
    created_at = _now_iso()
    payload = {
        "id": message_id,
        "session_id": normalized_session,
        "text": normalized_text,
        "intent": normalized_intent,
        "created_at": created_at,
        "expires_at": (datetime.now(UTC) + _MESSAGE_TTL).isoformat().replace("+00:00", "Z"),
    }
    runtime_dir = _antigravity_runtime_dir(config_dir)
    _ensure_private_dir(runtime_dir)
    _ensure_private_dir(runtime_dir / "inbox")
    path = antigravity_inbox_dir(normalized_session, config_dir) / f"msg-{message_id}.json"
    _write_private_json(path, payload)
    return {"message_id": message_id, "path": str(path), "payload": payload}


def wait_for_antigravity_message_claim(
    *,
    session_id: str,
    message_id: str,
    timeout_secs: float,
    config_dir: Path | None = None,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, float(timeout_secs))
    while time.monotonic() <= deadline:
        claimed = _claimed_message(session_id, message_id, config_dir=config_dir)
        if claimed is not None:
            return claimed
        time.sleep(0.05)
    return _claimed_message(session_id, message_id, config_dir=config_dir)


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
    )
    _write_text_if_changed(
        plugin_root / "plugin.json",
        json.dumps({"name": _ANTIGRAVITY_PLUGIN_NAME}, indent=2) + "\n",
    )
    _write_text_if_changed(hook_script, hook_content, mode=0o755)
    command_prefix = shlex.quote(str(hook_script))
    hook_config = _antigravity_hook_config(command_prefix)
    hooks = {_ANTIGRAVITY_PLUGIN_NAME: hook_config}
    if antigravity_bin:
        # Real agy loads both installed plugin hooks and global hooks. Keep the
        # plugin as an installable container, and register executable hooks once
        # through the global hooks file.
        _remove_file_if_exists(plugin_root / "hooks.json")
    else:
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
            message = "Could not install Longhouse Antigravity plugin"
            raise _AntigravityLaunchError(message + (f": {detail}" if detail else "."))
        _remove_file_if_exists(_antigravity_installed_plugin_hooks_path())
    return staged_root
