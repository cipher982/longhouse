"""Install the Longhouse-owned Cursor hook adapter without replacing user hooks."""

from __future__ import annotations

import json
import stat
from pathlib import Path

_EVENTS = (
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "afterAgentThought",
    "afterAgentResponse",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "stop",
)
_MARKER = "longhouse-cursor-hook.py"

_SCRIPT = r"""#!/usr/bin/env python3
import json, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
sid = os.environ.get("LONGHOUSE_SESSION_ID", "").strip()
conversation_id = str(payload.get("conversation_id") or "").strip()
if not sid or not conversation_id:
    print("{}")
    raise SystemExit(0)
home = Path(os.environ.get("LONGHOUSE_HOME") or (Path.home() / ".longhouse"))
root = home / "managed-local" / "cursor-helm"
events = root / "hook-events"
claims = root / "binding-probes"
events.mkdir(parents=True, exist_ok=True)
claims.mkdir(parents=True, exist_ok=True)
now = datetime.now(timezone.utc).isoformat()
row = {"event": event, "observed_at": now, "session_id": sid, "conversation_id": conversation_id, "payload": payload}
with (events / f"{sid}.ndjson").open("a", encoding="utf-8") as f:
    f.write(json.dumps(row, separators=(",", ":")) + "\n")
phase = "active" if event in {"beforeSubmitPrompt", "afterAgentThought", "preToolUse", "beforeShellExecution", "beforeMCPExecution"} else ("idle" if event in {"sessionStart", "stop", "afterAgentResponse"} else ("ended" if event == "sessionEnd" else None))
if phase:
    target = root / f"{sid}.phase.json"
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".phase.")
    with os.fdopen(fd, "w") as f:
        json.dump({"session_id": sid, "conversation_id": conversation_id, "phase": phase, "generation_id": payload.get("generation_id"), "observed_at": now}, f)
    os.replace(tmp, target)
claim = {"schema_version": 2, "provider": "cursor", "status": "observed", "session_id": sid, "conversation_uuid": conversation_id, "hook_observed_at": now}
target = claims / f"{sid}.json"
fd, tmp = tempfile.mkstemp(dir=claims, prefix=".claim.")
with os.fdopen(fd, "w") as f:
    json.dump(claim, f)
os.replace(tmp, target)
print("{}")
"""


def _is_ours(entry: object) -> bool:
    return isinstance(entry, dict) and _MARKER in str(entry.get("command") or "")


def install_cursor_hooks(cursor_dir: Path | None = None) -> list[str]:
    cursor_dir = cursor_dir or (Path.home() / ".cursor")
    if not cursor_dir.exists():
        return []
    hooks_dir = cursor_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = hooks_dir / _MARKER
    changed = not script.exists() or script.read_text(encoding="utf-8") != _SCRIPT
    if changed:
        script.write_text(_SCRIPT, encoding="utf-8")
    script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    config_path = cursor_dir / "hooks.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except json.JSONDecodeError:
        config = {}
    config["version"] = 1
    hooks = config.setdefault("hooks", {})
    for event in _EVENTS:
        existing = hooks.get(event)
        entries = existing if isinstance(existing, list) else []
        ours = {"command": f"{script} {event}", "timeout": 5, "failClosed": False}
        hooks[event] = [ours if _is_ours(item) else item for item in entries]
        if not any(_is_ours(item) for item in hooks[event]):
            hooks[event].append(ours)
    rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"
    config_changed = not config_path.exists() or config_path.read_text(encoding="utf-8") != rendered
    if config_changed:
        config_path.write_text(rendered, encoding="utf-8")
    return [f"Installed Cursor hooks in {cursor_dir}"] if changed or config_changed else [f"{config_path} already up to date"]
