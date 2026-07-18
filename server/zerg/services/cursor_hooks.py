"""Install the Longhouse-owned Cursor hook adapter without replacing user hooks."""

# ruff: noqa: E501 -- the embedded standalone hook is intentionally kept literal.

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
import hashlib, json, os, socket, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
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
launch_id = os.environ.get("LONGHOUSE_CURSOR_LAUNCH_ID", "").strip()
claim_target = claims / f"{sid}.json"
claim_backup = claims / f"{sid}.observed-backup.json"
existing_claim = None
try:
    existing_claim = json.loads(claim_target.read_text())
except (OSError, ValueError, TypeError):
    pass
claim_matches_reservation = (
    isinstance(existing_claim, dict)
    and existing_claim.get("session_id") == sid
    and existing_claim.get("conversation_uuid") == conversation_id
    and existing_claim.get("launch_id") == launch_id
    and existing_claim.get("status") in {"pending", "observed"}
)
if not claim_matches_reservation:
    if event in {"beforeShellExecution", "beforeMCPExecution"} and os.environ.get("LONGHOUSE_PERMISSION_HOOK_ENABLED") == "1":
        print(json.dumps({"permission": "deny", "user_message": "Longhouse launch identity mismatch; command blocked"}))
    else:
        print("{}")
    raise SystemExit(0)
row = {"event": event, "observed_at": now, "session_id": sid, "conversation_id": conversation_id, "payload": payload}
with (events / f"{sid}.ndjson").open("a", encoding="utf-8") as f:
    f.write(json.dumps(row, separators=(",", ":")) + "\n")
phase = "active" if event in {"beforeSubmitPrompt", "afterAgentThought", "preToolUse", "beforeShellExecution", "beforeMCPExecution"} else ("idle" if event in {"sessionStart", "stop", "afterAgentResponse"} else ("ended" if event == "sessionEnd" else None))
if phase:
    phase_target = root / f"{sid}.phase.json"
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".phase.")
    with os.fdopen(fd, "w") as f:
        json.dump({"session_id": sid, "conversation_id": conversation_id, "launch_id": launch_id, "phase": phase, "generation_id": payload.get("generation_id"), "observed_at": now}, f)
    os.replace(tmp, phase_target)
    outbox = home / "agent" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=outbox, prefix=".tmp.")
    with os.fdopen(fd, "w") as f:
        json.dump({"session_id": sid, "state": "thinking" if phase == "active" else "idle", "tool_name": payload.get("tool_name"), "cwd": payload.get("cwd"), "provider": "cursor", "control_path": "managed"}, f)
    os.replace(tmp, outbox / (Path(tmp).name.replace(".tmp.", "prs.") + ".json"))
claim = {
    "schema_version": 2,
    "provider": "cursor",
    "status": "observed",
    "session_id": sid,
    "conversation_uuid": conversation_id,
    "launch_id": launch_id,
    "hook_observed_at": now,
}
for key in ("thread_id", "turn_id", "run_id", "client_request_id"):
    if isinstance(existing_claim, dict) and existing_claim.get(key):
        claim[key] = existing_claim[key]
registration_ready = os.environ.get("LONGHOUSE_CURSOR_REGISTRATION_READY") == "1"
try:
    if not registration_ready:
        launch_state = json.loads((root / f"{sid}.json").read_text())
        registration_ready = launch_state.get("registration") == "registered"
except (OSError, ValueError, TypeError):
    pass
if registration_ready:
    fd, tmp = tempfile.mkstemp(dir=claims, prefix=".claim.")
    with os.fdopen(fd, "w") as f:
        json.dump(claim, f)
    os.replace(tmp, claim_target)
    claim_backup.unlink(missing_ok=True)

if event in {"afterAgentResponse", "stop"}:
    cursor_home = Path(os.environ.get("CURSOR_HOME") or (Path.home() / ".cursor"))
    stores = list((cursor_home / "chats").glob(f"*/{conversation_id}/store.db"))
    wake_socket = home / "agent" / "transcript-wake.sock"
    if len(stores) == 1 and wake_socket.exists():
        store = stores[0]
        wake = {
            "provider": "cursor",
            "path": str(store),
            "phase": "idle",
            "session_id": sid,
            "turn_id": payload.get("generation_id"),
            "wake_reason": "turn_completed",
            "observed_at_ms": int(time.time() * 1000),
            "file_len_hint": store.stat().st_size,
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(0.075)
                stream.connect(str(wake_socket))
                stream.sendall(json.dumps(wake, separators=(",", ":")).encode())
        except OSError:
            pass

def permission(value, message=None):
    result = {"permission": value}
    if message:
        result["user_message"] = message
    print(json.dumps(result))

if event in {"beforeShellExecution", "beforeMCPExecution"} and os.environ.get("LONGHOUSE_PERMISSION_HOOK_ENABLED") == "1":
    base = os.environ.get("LONGHOUSE_HOOK_URL", "").rstrip("/")
    token = os.environ.get("LONGHOUSE_HOOK_TOKEN", "")
    invocation_id = str(payload.get("tool_call_id") or payload.get("toolCallId") or payload.get("invocation_id") or payload.get("call_id") or f"{now}:{os.getpid()}")
    material = "|".join([conversation_id, str(payload.get("generation_id") or ""), event, invocation_id, str(payload.get("command") or payload.get("tool_name") or "")])
    request_id = hashlib.sha256(material.encode()).hexdigest()
    tool_name = "Shell" if event == "beforeShellExecution" else str(payload.get("tool_name") or "MCP")
    tool_input = {"command": payload.get("command")} if event == "beforeShellExecution" else (payload.get("tool_input") or payload.get("arguments") or {})
    body = json.dumps({"session_id": sid, "tool_use_id": request_id, "tool_name": tool_name, "tool_input": tool_input, "provider": "cursor"}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Agents-Token": token,
        "User-Agent": "Longhouse-Cursor-Hook/1",
    }
    try:
        req = urllib.request.Request(base + "/api/agents/permission-requests", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            ack = json.loads(response.read().decode() or "{}")
        pause_id = str(ack.get("pause_request_id") or "")
        deadline = time.monotonic() + min(20.0, max(0.0, float(os.environ.get("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", "20"))))
        query = urllib.parse.urlencode({"session_id": sid, "tool_use_id": request_id, "pause_request_id": pause_id})
        while pause_id and time.monotonic() < deadline:
            req = urllib.request.Request(base + "/api/agents/permission-decision?" + query, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode() or "{}")
            if result.get("resolved"):
                decision = str(result.get("decision") or "ask").lower()
                permission(decision if decision in {"allow", "deny"} else "ask", result.get("reason"))
                raise SystemExit(0)
            time.sleep(0.5)
    except (OSError, ValueError, urllib.error.URLError):
        pass
    permission("deny", "Longhouse approval unavailable; command blocked")
else:
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
        timeout = 25 if event in {"beforeShellExecution", "beforeMCPExecution"} else 5
        permission_event = event in {"beforeShellExecution", "beforeMCPExecution"}
        ours = {"command": f"{script} {event}", "timeout": timeout, "failClosed": permission_event}
        hooks[event] = [ours if _is_ours(item) else item for item in entries]
        if not any(_is_ours(item) for item in hooks[event]):
            hooks[event].append(ours)
    rendered = json.dumps(config, indent=2, sort_keys=True) + "\n"
    config_changed = not config_path.exists() or config_path.read_text(encoding="utf-8") != rendered
    if config_changed:
        config_path.write_text(rendered, encoding="utf-8")
    return [f"Installed Cursor hooks in {cursor_dir}"] if changed or config_changed else [f"{config_path} already up to date"]
