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

from zerg.services.longhouse_paths import get_managed_local_dir
from zerg.services.longhouse_paths import resolve_longhouse_home

_ANTIGRAVITY_PLUGIN_NAME = "longhouse-runtime"
_ANTIGRAVITY_HOOK_SCRIPT_NAME = "longhouse-antigravity-hook.sh"
_MESSAGE_TTL = timedelta(minutes=5)
_MAX_MESSAGE_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


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

PROBE_ERR=$("$PYTHON" -c 'import json' 2>&1)
if [ $? -ne 0 ]; then
  printf 'longhouse-antigravity-hook: python probe failed: %s
' "$PROBE_ERR" >&2
  emit_default_response
  exit 0
fi

LONGHOUSE_HOOK_INPUT="$INPUT" \\
LONGHOUSE_HOOK_EVENT="$EVENT" \\
LONGHOUSE_HOOK_HOME="$LONGHOUSE_HOME" \\
LONGHOUSE_HOOK_ENGINE="$ENGINE" \\
"$PYTHON" - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_private_json(path: Path, payload: dict) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.write("\\n")
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


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


def write_session_state(state_dir: str, payload: dict) -> None:
    if not state_dir or not payload.get("session_id"):
        return
    try:
        write_private_json(Path(state_dir) / f"{payload['session_id']}.json", payload)
    except Exception:
        pass


def default_antigravity_state_dir(longhouse_home: str) -> str:
    if not longhouse_home:
        return ""
    return str(Path(longhouse_home) / "managed-local" / "antigravity" / "sessions")


def default_antigravity_inbox_dir(longhouse_home: str, session_id: str) -> str:
    if not longhouse_home or not session_id:
        return ""
    return str(Path(longhouse_home) / "managed-local" / "antigravity" / "inbox" / session_id)


def pending_message_paths(inbox_dir: str) -> list[Path]:
    if not inbox_dir:
        return []
    try:
        root = Path(inbox_dir)
        if root.exists():
            ensure_private_dir(root)
        return sorted(root.glob("msg-*.json"), key=lambda path: path.name)
    except Exception:
        return []


def is_safe_message_path(path: Path) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file():
        return False
    if hasattr(os, "geteuid") and stat.st_uid != os.geteuid():
        return False
    return (stat.st_mode & 0o077) == 0


def read_message(path: Path) -> dict | None:
    if not is_safe_message_path(path):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def inbox_has_pending(inbox_dir: str) -> bool:
    return bool(pending_message_paths(inbox_dir))


def claim_inbox_messages(
    *,
    inbox_dir: str,
    session_id: str,
    event: str,
    conversation_id: str,
    step_index: str,
    limit: int = 4,
) -> list[str]:
    texts: list[str] = []
    if not inbox_dir or not session_id:
        return texts
    claimed_dir = Path(inbox_dir) / "claimed"
    for path in pending_message_paths(inbox_dir)[:limit]:
        payload = read_message(path)
        if not payload:
            continue
        expires_at = parse_utc(str(payload.get("expires_at") or ""))
        if expires_at is not None and expires_at < datetime.now(timezone.utc):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        text = str(payload.get("text") or "")
        if not text.strip():
            continue
        message_id = str(payload.get("id") or path.stem.replace("msg-", ""))
        claim_path = claimed_dir / f"claimed-{path.name}"
        try:
            ensure_private_dir(claimed_dir)
            path.replace(claim_path)
        except OSError:
            continue
        payload.update(
            {
                "id": message_id,
                "session_id": session_id,
                "claimed_at": now_iso(),
                "claimed_by": "longhouse-antigravity-hook",
                "hook_event": event,
                "conversation_id": conversation_id,
                "step_index": step_index,
            }
        )
        try:
            write_private_json(claim_path, payload)
        except Exception:
            pass
        texts.append(text)
    return texts


def hook_response(event: str, texts: list[str], inbox_dir: str) -> dict:
    if event == "PreInvocation":
        return {"injectSteps": [{"userMessage": text} for text in texts]}
    if event == "PostInvocation":
        return {
            "injectSteps": [{"userMessage": text} for text in texts],
            "terminationBehavior": "force_continue" if texts else "",
        }
    if event == "Stop" and inbox_has_pending(inbox_dir):
        return {
            "decision": "continue",
            "reason": "Longhouse queued input is waiting in the managed Antigravity inbox.",
        }
    return default_response(event)


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
longhouse_home = os.environ.get("LONGHOUSE_HOOK_HOME", "")
state_dir = os.environ.get("LONGHOUSE_ANTIGRAVITY_STATE_DIR") or default_antigravity_state_dir(longhouse_home)
inbox_dir = os.environ.get("LONGHOUSE_ANTIGRAVITY_INBOX_DIR") or default_antigravity_inbox_dir(
    longhouse_home,
    session_id,
)

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
    write_session_state(
        state_dir,
        {
            "session_id": session_id,
            "provider_session_id": conversation_id,
            "conversation_id": conversation_id,
            "cwd": cwd,
            "transcript_path": transcript,
            "step_index": step_index,
            "state": state,
            "updated_at": now_iso(),
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

claimed_texts: list[str] = []
if event in {"PreInvocation", "PostInvocation"}:
    claimed_texts = claim_inbox_messages(
        inbox_dir=inbox_dir,
        session_id=session_id,
        event=event,
        conversation_id=conversation_id,
        step_index=step_index,
    )

print(json.dumps(hook_response(event, claimed_texts, inbox_dir), separators=(",", ":")))
PY
HOOK_RC=$?
if [ "$HOOK_RC" -ne 0 ]; then
  printf 'longhouse-antigravity-hook: python hook failed with exit %s
' "$HOOK_RC" >&2
  emit_default_response
fi
exit 0
"""


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


def _default_engine_path() -> str:
    try:
        from zerg.services.shipper.service import get_engine_executable

        return get_engine_executable()
    except RuntimeError:
        return "longhouse-engine"


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
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


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
