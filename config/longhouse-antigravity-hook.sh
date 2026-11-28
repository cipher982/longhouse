#!/bin/bash
# Longhouse Antigravity hook - local presence/inbox boundary.
INPUT=$(/bin/cat)
EVENT="${1:-}"
LONGHOUSE_HOME="${LONGHOUSE_HOME:-}"
if [ -z "$LONGHOUSE_HOME" ]; then
  LONGHOUSE_HOME=__LONGHOUSE_HOME__
fi
PYTHON="${LONGHOUSE_HOOK_PYTHON:-python3}"

emit_default_response() {
  case "$EVENT" in
    PreInvocation) printf '{"injectSteps":[]}\n' ;;
    PostInvocation) printf '{"injectSteps":[],"terminationBehavior":""}\n' ;;
    Stop) printf '{"decision":"allow","reason":""}\n' ;;
    *) printf '{}\n' ;;
  esac
}

LONGHOUSE_HOOK_INPUT="$INPUT" \
LONGHOUSE_HOOK_EVENT="$EVENT" \
LONGHOUSE_HOOK_HOME="$LONGHOUSE_HOME" \
"$PYTHON" - <<'PY'
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
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
            file.write("\n")
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
    try:
        outbox.chmod(0o700)
    except OSError:
        return
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp.", dir=str(outbox))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.write("\n")
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
        path = Path(state_dir) / f"{payload['session_id']}.json"
        existing = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass
        existing.update(payload)
        write_private_json(path, existing)
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


def _is_slash_command(text: str) -> bool:
    # `/name args` as engine/src/slash_input.rs reads it; `//text` is a message.
    stripped = text.lstrip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return False
    name = stripped[1:].split(None, 1)[0] if stripped[1:].strip() else ""
    return bool(name) and all(c.isalnum() or c in "_-:." for c in name)


def claim_inbox_messages(
    *,
    inbox_dir: str,
    session_id: str,
    event: str,
    conversation_id: str,
    step_index: str,
    limit: int = 4,
) -> list[dict]:
    receipts: list[dict] = []
    if not inbox_dir or not session_id:
        return receipts
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
        if _is_slash_command(text):
            # A command is typed into agy's terminal by the launcher when agy is
            # idle, as the owner would type it; injected here it would reach
            # the model as text.
            continue
        message_id = str(payload.get("id") or path.stem.replace("msg-", ""))
        claim_path = claimed_dir / f"claimed-{path.name}"
        try:
            ensure_private_dir(claimed_dir)
            path.replace(claim_path)
        except OSError:
            continue
        claimed_at = now_iso()
        payload.update(
            {
                "id": message_id,
                "session_id": session_id,
                "claimed_at": claimed_at,
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
        receipts.append(
            {
                "id": message_id,
                "text": text,
                "claimed_at": claimed_at,
                "intent": str(payload.get("intent") or "send"),
            }
        )
    return receipts


def hook_response(event: str, texts: list[str], inbox_dir: str, terminate: bool = False) -> dict:
    # An interrupt ends the turn at this invocation boundary: agy finishes the
    # tool call in flight, then the next model call never happens.
    if terminate and event in {"PreInvocation", "PostInvocation"}:
        return {"injectSteps": [], "terminationBehavior": "terminate"}
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
# The short transcript truncates tool output; always select full native history.
# Keep presence, managed binding intent, and the completion wake on one source.
transcript_hint = Path(transcript)
if (
    transcript_hint.name == "transcript.jsonl"
    and transcript_hint.parent.name == "logs"
    and transcript_hint.parent.parent.name == ".system_generated"
):
    transcript = str(transcript_hint.with_name("transcript_full.jsonl"))
step_index = str(data.get("stepIdx") or data.get("step_index") or "")
managed_provider = (os.environ.get("LONGHOUSE_MANAGED_PROVIDER") or "").strip().lower()
# Antigravity ACP Console owns the coordinator's identity, transcript binding,
# live phase, and steering over JSON-RPC. Its child process tree (and any
# /swarm workers or subagents it spawns) inherits LONGHOUSE_MANAGED_* env vars;
# running the Helm hook here overwrites session_phase_state with run_id=None
# and aliases worker conversations onto the coordinator session.
if (os.environ.get("LONGHOUSE_ANTIGRAVITY_ACP_MODE") or "").strip() == "1":
    print(json.dumps(default_response(event), separators=(",", ":")), flush=True)
    raise SystemExit(0)
# Provider subprocesses may inherit a parent Helm session environment. A
# session id is authoritative only when the provider identity agrees; binding
# an Antigravity transcript to an inherited Claude id corrupts source identity.
managed_session_id = (
    os.environ.get("LONGHOUSE_MANAGED_SESSION_ID") or ""
) if managed_provider == "antigravity" else ""
longhouse_home = os.environ.get("LONGHOUSE_HOOK_HOME", "")
state_dir = os.environ.get("LONGHOUSE_ANTIGRAVITY_STATE_DIR") or default_antigravity_state_dir(longhouse_home)
# A Helm Antigravity session binds to one native conversation id. Swarm workers
# and subagents spawned inside that Helm process inherit LONGHOUSE_MANAGED_*
# and LONGHOUSE_ANTIGRAVITY_INBOX_DIR; if the state file is already bound to a
# different conversation id, do not hijack the parent session or claim its inbox.
inherited_child_conversation = False
if managed_session_id and conversation_id and state_dir:
    try:
        existing_state = json.loads((Path(state_dir) / f"{managed_session_id}.json").read_text(encoding="utf-8"))
        existing_conversation_id = str(existing_state.get("conversation_id") or "") if isinstance(existing_state, dict) else ""
        if existing_conversation_id and existing_conversation_id != conversation_id:
            managed_session_id = ""
            inherited_child_conversation = True
    except Exception:
        pass
session_id = managed_session_id or conversation_id
inbox_dir = "" if inherited_child_conversation else (
    os.environ.get("LONGHOUSE_ANTIGRAVITY_INBOX_DIR") or default_antigravity_inbox_dir(
        longhouse_home,
        session_id,
    )
)

state = ""
if event == "SessionStart":
    state = "idle"
elif event == "PreInvocation":
    state = "thinking"
elif event == "PreToolUse":
    state = "running"
elif event in {"PostToolUse", "PostInvocation"}:
    state = "thinking"
elif event == "Stop":
    fully_idle = data.get("fullyIdle")
    state = "idle" if fully_idle is True or str(fully_idle).lower() in {"1", "true"} else "running"

if state and session_id:
    hook_observed_at = now_iso()
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
            "control_path": "managed" if managed_session_id else "unmanaged",
        },
    )
    write_session_state(
        state_dir,
        {
            "schema_version": 2,
            "session_id": session_id,
            "provider_session_id": conversation_id,
            "conversation_id": conversation_id,
            "cwd": cwd,
            "transcript_path": transcript,
            "step_index": step_index,
            "state": state,
            "updated_at": hook_observed_at,
            "last_hook_event": event,
            "last_hook_observed_at": hook_observed_at,
        },
    )
    if event == "Stop" and transcript:
        wake_socket = Path(longhouse_home) / "agent" / "transcript-wake.sock"
        wake = {
            "provider": "antigravity",
            "path": transcript,
            "phase": "idle",
            "session_id": session_id,
            "turn_id": step_index,
            "wake_reason": "turn_completed",
            "observed_at_ms": int(time.time() * 1000),
        }
        try:
            wake["file_len_hint"] = Path(transcript).stat().st_size
        except OSError:
            pass
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(0.075)
                stream.connect(str(wake_socket))
                stream.sendall(json.dumps(wake, separators=(",", ":")).encode())
        except OSError:
            pass
claimed_receipts: list[dict] = []
if event in {"PreInvocation", "PostInvocation"}:
    claimed_receipts = claim_inbox_messages(
        inbox_dir=inbox_dir,
        session_id=session_id,
        event=event,
        conversation_id=conversation_id,
        step_index=step_index,
    )

interrupt_requested = any(receipt.get("intent") == "interrupt" for receipt in claimed_receipts)
claimed_texts = [
    str(receipt.get("text") or "") for receipt in claimed_receipts if receipt.get("intent") != "interrupt"
]
response = hook_response(event, claimed_texts, inbox_dir, interrupt_requested)
print(json.dumps(response, separators=(",", ":")), flush=True)
if session_id and claimed_receipts:
    last_receipt = claimed_receipts[-1]
    response_at = now_iso()
    write_session_state(
        state_dir,
        {
            "schema_version": 2,
            "session_id": session_id,
            "updated_at": response_at,
            "last_claimed_message_id": str(last_receipt.get("id") or ""),
            "last_claimed_at": str(last_receipt.get("claimed_at") or ""),
            "last_claim_event": event,
            "last_claim_conversation_id": conversation_id,
            "last_claim_step_index": step_index,
            "last_response_event": event,
            "last_response_at": response_at,
            "last_response_status": "ok",
            "last_response_claimed_message_ids": [
                str(receipt.get("id") or "") for receipt in claimed_receipts
            ],
            "last_continuation_requested": (
                response.get("terminationBehavior") == "force_continue"
                or response.get("decision") == "continue"
            ),
        },
    )
PY
HOOK_RC=$?
if [ "$HOOK_RC" -ne 0 ]; then
  printf 'longhouse-antigravity-hook: local hook failed with exit %s; continuing provider execution
' "$HOOK_RC" >&2
  emit_default_response
  exit 0
fi
exit 0
