#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOSTED_INSTANCE_HELPER="${HOSTED_INSTANCE_HELPER:-$ROOT_DIR/scripts/lib/hosted-instance.sh}"

if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"

INSTANCE_SUBDOMAIN="david010"
TARGET_CWD="$ROOT_DIR"
TURN_COUNT=4
DELAY_SECS="0"
PROMPT_PREFIX="lh-hosted-claude-stress"
PROJECT_NAME="hosted-managed-local-claude-stress"
DISPLAY_NAME="Hosted Managed Local Claude Stress"
LOOP_MODE="assist"
CHAT_TIMEOUT_SECS="180"
VERIFY_TIMEOUT_SECS="30"
SSH_TARGET="zerg"
MACHINE_NAME="${MACHINE_NAME:-}"
if [[ -z "$MACHINE_NAME" && -f "$HOME/.claude/longhouse-machine-name" ]]; then
  MACHINE_NAME="$(tr -d '\r\n' < "$HOME/.claude/longhouse-machine-name")"
fi

usage() {
  cat <<'EOF'
Usage:
  scripts/hosted-managed-local-claude-stress.sh [options]

Launch a real hosted managed-local Claude session on this device, then send
repeated simple one-line prompts through the real `/api/sessions/{id}/chat`
route and verify each turn against hosted session events.

Requirements:
  - CONTROL_PLANE_ADMIN_TOKEN or ADMIN_TOKEN
  - this machine already connected as a runner to the target tenant
  - uv available locally

Options:
  --subdomain <name>        Hosted instance subdomain (default: david010)
  --cwd <path>              Working directory for the managed-local launch
  --count <n>               Number of prompts to send (default: 4)
  --delay-secs <n>          Delay between turns (default: 0)
  --prompt-prefix <text>    Prefix for generated one-line prompts
  --project <name>          Project label for launch
  --display-name <name>     Display name for launch
  --loop-mode <mode>        manual|assist|autopilot (default: assist)
  --chat-timeout-secs <n>   Per-turn SSE read timeout (default: 180)
  --verify-timeout-secs <n> Poll timeout for hosted events verification (default: 30)
  --ssh-target <host>       SSH target for hosted SQLite fallback (default: zerg)
  --machine-name <name>     Optional explicit Longhouse machine label override
  -h, --help                Show help
EOF
}

while (($# > 0)); do
  case "$1" in
    --subdomain)
      [[ -n "${2:-}" ]] || { echo "--subdomain requires a value" >&2; exit 1; }
      INSTANCE_SUBDOMAIN="$2"
      shift 2
      ;;
    --cwd)
      [[ -n "${2:-}" ]] || { echo "--cwd requires a value" >&2; exit 1; }
      TARGET_CWD="$2"
      shift 2
      ;;
    --count)
      [[ -n "${2:-}" ]] || { echo "--count requires a value" >&2; exit 1; }
      TURN_COUNT="$2"
      shift 2
      ;;
    --delay-secs)
      [[ -n "${2:-}" ]] || { echo "--delay-secs requires a value" >&2; exit 1; }
      DELAY_SECS="$2"
      shift 2
      ;;
    --prompt-prefix)
      [[ -n "${2:-}" ]] || { echo "--prompt-prefix requires a value" >&2; exit 1; }
      PROMPT_PREFIX="$2"
      shift 2
      ;;
    --project)
      [[ -n "${2:-}" ]] || { echo "--project requires a value" >&2; exit 1; }
      PROJECT_NAME="$2"
      shift 2
      ;;
    --display-name)
      [[ -n "${2:-}" ]] || { echo "--display-name requires a value" >&2; exit 1; }
      DISPLAY_NAME="$2"
      shift 2
      ;;
    --loop-mode)
      [[ -n "${2:-}" ]] || { echo "--loop-mode requires a value" >&2; exit 1; }
      LOOP_MODE="$2"
      shift 2
      ;;
    --chat-timeout-secs)
      [[ -n "${2:-}" ]] || { echo "--chat-timeout-secs requires a value" >&2; exit 1; }
      CHAT_TIMEOUT_SECS="$2"
      shift 2
      ;;
    --verify-timeout-secs)
      [[ -n "${2:-}" ]] || { echo "--verify-timeout-secs requires a value" >&2; exit 1; }
      VERIFY_TIMEOUT_SECS="$2"
      shift 2
      ;;
    --ssh-target)
      [[ -n "${2:-}" ]] || { echo "--ssh-target requires a value" >&2; exit 1; }
      SSH_TARGET="$2"
      shift 2
      ;;
    --machine-name)
      [[ -n "${2:-}" ]] || { echo "--machine-name requires a value" >&2; exit 1; }
      MACHINE_NAME="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! [[ "$TURN_COUNT" =~ ^[0-9]+$ ]] || [[ "$TURN_COUNT" -lt 1 ]]; then
  echo "--count must be a positive integer" >&2
  exit 1
fi

TARGET_CWD="$(cd "$TARGET_CWD" && pwd)"

lh_hosted_prepare_target "$INSTANCE_SUBDOMAIN" "" "" "david010"
API_URL="$LH_TARGET_API_URL"
INSTANCE_URL="$LH_TARGET_FRONTEND_URL"

LH_STRESS_ACCESS_TOKEN=""
LH_STRESS_DEVICE_TOKEN_ID=""
LH_STRESS_DEVICE_TOKEN=""

cleanup() {
  if [[ -n "$LH_STRESS_DEVICE_TOKEN_ID" && -n "$LH_STRESS_ACCESS_TOKEN" ]]; then
    if ! lh_hosted_revoke_device_token "$LH_STRESS_ACCESS_TOKEN" "$LH_STRESS_DEVICE_TOKEN_ID" "$API_URL" >/dev/null 2>&1; then
      echo "Warning: failed to revoke hosted stress device token $LH_STRESS_DEVICE_TOKEN_ID" >&2
    fi
  fi
}
trap cleanup EXIT

echo "Target tenant: $INSTANCE_SUBDOMAIN ($INSTANCE_URL)" >&2
LH_STRESS_ACCESS_TOKEN="$(lh_hosted_exchange_login_token "$(lh_hosted_issue_login_token "$LH_INSTANCE_ID")" "$API_URL")"
IFS=$'\t' read -r LH_STRESS_DEVICE_TOKEN_ID LH_STRESS_DEVICE_TOKEN <<< \
  "$(lh_hosted_create_device_token "$LH_STRESS_ACCESS_TOKEN" "$API_URL" "hosted-claude-stress-${INSTANCE_SUBDOMAIN}-${RANDOM}")"

export LH_STRESS_API_URL="$API_URL"
export LH_STRESS_ACCESS_TOKEN
export LH_STRESS_DEVICE_TOKEN
export LH_STRESS_TARGET_CWD="$TARGET_CWD"
export LH_STRESS_INSTANCE_SUBDOMAIN="$INSTANCE_SUBDOMAIN"
export LH_STRESS_TURN_COUNT="$TURN_COUNT"
export LH_STRESS_DELAY_SECS="$DELAY_SECS"
export LH_STRESS_PROMPT_PREFIX="$PROMPT_PREFIX"
export LH_STRESS_PROJECT_NAME="$PROJECT_NAME"
export LH_STRESS_DISPLAY_NAME="$DISPLAY_NAME"
export LH_STRESS_LOOP_MODE="$LOOP_MODE"
export LH_STRESS_CHAT_TIMEOUT_SECS="$CHAT_TIMEOUT_SECS"
export LH_STRESS_VERIFY_TIMEOUT_SECS="$VERIFY_TIMEOUT_SECS"
export LH_STRESS_SSH_TARGET="$SSH_TARGET"
export LH_STRESS_MACHINE_NAME="$MACHINE_NAME"

cd "$ROOT_DIR"
uv run --project server python -u <<'PY'
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable
from typing import Iterator

import httpx


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str


def parse_sse_lines(lines: Iterable[str]) -> Iterator[SSEEvent]:
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if not line:
            if data_lines:
                yield SSEEvent(event=event_name, data="\n".join(data_lines))
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip() or "message"
            continue
        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    if data_lines:
        yield SSEEvent(event=event_name, data="\n".join(data_lines))


def build_prompts(*, count: int, prefix: str) -> list[tuple[str, str]]:
    nonce = secrets.token_hex(4)
    prompts: list[tuple[str, str]] = []
    for idx in range(1, count + 1):
        token = f"{prefix}-{idx:02d}-{nonce}"
        prompts.append((f"Reply with exactly {token} and nothing else.", token))
    return prompts


def fetch_events_via_api(*, client: httpx.Client, api_url: str, device_token: str, session_id: str) -> list[dict]:
    response = client.get(
        f"{api_url.rstrip('/')}/api/agents/sessions/{session_id}/events",
        headers={"X-Agents-Token": device_token},
        params={"limit": 1000, "branch_mode": "head", "context_mode": "forensic"},
    )
    response.raise_for_status()
    payload = response.json()
    events = payload.get("events", [])
    if not isinstance(events, list):
        raise RuntimeError("Events payload missing list")
    return events


def fetch_events_via_ssh(*, ssh_target: str, subdomain: str, session_id: str, prompt: str, token: str) -> list[dict]:
    if shutil.which("ssh") is None:
        raise RuntimeError("ssh is not available for hosted fallback verification")

    remote_script = f"""
python3 - <<'__LH_REMOTE_PY__'
import json
import sqlite3

session_id = {session_id!r}
prompt = {prompt!r}
token = {token!r}
conn = sqlite3.connect('/data/longhouse.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    \"\"\"
    SELECT id, role, content_text
    FROM events
    WHERE session_id = ?
      AND (
        (role = 'user' AND content_text = ?)
        OR
        (role = 'assistant' AND instr(content_text, ?) > 0)
      )
    ORDER BY id DESC
    LIMIT 100
    \"\"\",
    [session_id, prompt, token],
).fetchall()
print(json.dumps([dict(row) for row in rows]))
__LH_REMOTE_PY__
"""
    container_name = f"longhouse-{subdomain}"
    completed = subprocess.run(
        [
            "ssh",
            ssh_target,
            f"docker exec -i {shlex.quote(container_name)} bash -lc {shlex.quote(remote_script)}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "hosted ssh fallback failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list):
        raise RuntimeError("hosted ssh fallback returned invalid payload")
    return payload


def fetch_events(
    *,
    client: httpx.Client,
    api_url: str,
    device_token: str,
    session_id: str,
    ssh_target: str,
    subdomain: str,
    prompt: str,
    token: str,
) -> list[dict]:
    try:
        return fetch_events_via_api(
            client=client,
            api_url=api_url,
            device_token=device_token,
            session_id=session_id,
        )
    except Exception:
        return fetch_events_via_ssh(
            ssh_target=ssh_target,
            subdomain=subdomain,
            session_id=session_id,
            prompt=prompt,
            token=token,
        )


def count_exact_user_events(events: list[dict], prompt: str) -> int:
    return sum(1 for event in events if event.get("role") == "user" and event.get("content_text") == prompt)


def summarize_new_batch(events: list[dict], *, after_event_id: int, prompt: str, token: str) -> tuple[int, list[str], int]:
    exact_user = 0
    assistant_messages: list[str] = []
    max_event_id = after_event_id
    for event in events:
        event_id = int(event.get("id") or 0)
        if event_id <= after_event_id:
            continue
        max_event_id = max(max_event_id, event_id)
        if event.get("role") == "user" and event.get("content_text") == prompt:
            exact_user += 1
        if event.get("role") == "assistant" and event.get("content_text") and token in str(event["content_text"]):
            assistant_messages.append(str(event["content_text"]))
    return exact_user, assistant_messages, max_event_id


def main() -> int:
    api_url = os.environ["LH_STRESS_API_URL"]
    access_token = os.environ["LH_STRESS_ACCESS_TOKEN"]
    device_token = os.environ["LH_STRESS_DEVICE_TOKEN"]
    cwd = os.environ["LH_STRESS_TARGET_CWD"]
    subdomain = os.environ["LH_STRESS_INSTANCE_SUBDOMAIN"]
    turn_count = int(os.environ["LH_STRESS_TURN_COUNT"])
    delay_secs = float(os.environ["LH_STRESS_DELAY_SECS"])
    prompt_prefix = os.environ["LH_STRESS_PROMPT_PREFIX"]
    project_name = os.environ["LH_STRESS_PROJECT_NAME"]
    display_name = os.environ["LH_STRESS_DISPLAY_NAME"]
    loop_mode = os.environ["LH_STRESS_LOOP_MODE"]
    chat_timeout_secs = float(os.environ["LH_STRESS_CHAT_TIMEOUT_SECS"])
    verify_timeout_secs = float(os.environ["LH_STRESS_VERIFY_TIMEOUT_SECS"])
    machine_name = os.environ.get("LH_STRESS_MACHINE_NAME", "").strip()
    ssh_target = os.environ.get("LH_STRESS_SSH_TARGET", "zerg").strip() or "zerg"

    timeout = httpx.Timeout(connect=20.0, read=chat_timeout_secs, write=20.0, pool=20.0)
    with httpx.Client(timeout=timeout) as client:
        health = client.get(f"{api_url.rstrip('/')}/api/health")
        if health.status_code != 200:
            raise RuntimeError(f"health status={health.status_code} body={health.text[:400]}")

        launch_body: dict[str, object] = {
            "cwd": cwd,
            "provider": "claude",
            "project": project_name,
            "display_name": display_name,
            "loop_mode": loop_mode,
        }
        if machine_name:
            launch_body["machine_name"] = machine_name

        launch = client.post(
            f"{api_url.rstrip('/')}/api/sessions/managed-local/this-device",
            headers={"X-Agents-Token": device_token},
            json=launch_body,
        )
        if launch.status_code != 200:
            raise RuntimeError(f"launch status={launch.status_code} body={launch.text[:400]}")
        launch_payload = launch.json()
        session_id = str(launch_payload["session_id"])
        print(f"Tenant: {subdomain}")
        print(f"Session: {session_id}")
        print(f"Attach: {launch_payload['attach_command']}")

        prompts = build_prompts(count=turn_count, prefix=prompt_prefix)

        for idx, (prompt, token) in enumerate(prompts, start=1):
            before_events = fetch_events(
                client=client,
                api_url=api_url,
                device_token=device_token,
                session_id=session_id,
                ssh_target=ssh_target,
                subdomain=subdomain,
                prompt=prompt,
                token=token,
            )
            before_exact = count_exact_user_events(before_events, prompt)
            before_event_id = max((int(event.get("id") or 0) for event in before_events), default=0)

            sse_error = None
            done_payload = None
            status_code = 0
            try:
                with client.stream(
                    "POST",
                    f"{api_url.rstrip('/')}/api/sessions/{session_id}/chat",
                    params={"token": access_token},
                    json={"message": prompt},
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    status_code = response.status_code
                    if status_code != 200:
                        preview = response.read().decode("utf-8", errors="replace")[:400]
                        raise RuntimeError(f"chat status={status_code} body={preview}")
                    for event in parse_sse_lines(response.iter_lines()):
                        if event.event == "error":
                            try:
                                parsed = json.loads(event.data)
                                sse_error = str(parsed.get("error") or event.data)
                            except json.JSONDecodeError:
                                sse_error = event.data
                        elif event.event == "done":
                            try:
                                parsed_done = json.loads(event.data)
                                if isinstance(parsed_done, dict):
                                    done_payload = parsed_done
                            except json.JSONDecodeError:
                                done_payload = {"raw": event.data}
            except Exception as exc:
                sse_error = f"{type(exc).__name__}: {exc}"

            deadline = time.monotonic() + verify_timeout_secs
            exact_in_new_batch = 0
            assistant_messages: list[str] = []
            after_event_id = before_event_id
            while time.monotonic() < deadline:
                after_events = fetch_events(
                    client=client,
                    api_url=api_url,
                    device_token=device_token,
                    session_id=session_id,
                    ssh_target=ssh_target,
                    subdomain=subdomain,
                    prompt=prompt,
                    token=token,
                )
                exact_in_new_batch, assistant_messages, after_event_id = summarize_new_batch(
                    after_events,
                    after_event_id=before_event_id,
                    prompt=prompt,
                    token=token,
                )
                if exact_in_new_batch == 1 and (assistant_messages or (done_payload and int(done_payload.get("persisted_events", 0)) > 0)):
                    break
                time.sleep(1.0)

            total_exact = count_exact_user_events(
                fetch_events(
                    client=client,
                    api_url=api_url,
                    device_token=device_token,
                    session_id=session_id,
                    ssh_target=ssh_target,
                    subdomain=subdomain,
                    prompt=prompt,
                    token=token,
                ),
                prompt,
            )
            persisted_events = int(done_payload.get("persisted_events", 0)) if isinstance(done_payload, dict) else 0
            ok = (
                status_code == 200
                and sse_error is None
                and exact_in_new_batch == 1
                and total_exact == before_exact + 1
                and (assistant_messages or persisted_events > 0)
            )
            label = "ok" if ok else "fail"
            print(
                f"[{idx}/{len(prompts)}] {label} status={status_code} prompt={prompt!r} "
                f"token={token} new_exact={exact_in_new_batch} total_exact={total_exact} persisted={persisted_events} "
                f"assistant_events={len(assistant_messages)}"
            )
            if assistant_messages:
                print(f"  assistant: {assistant_messages[0][:160]}")
            if sse_error:
                print(f"  sse_error: {sse_error}")
            if not ok:
                return 1
            if delay_secs:
                time.sleep(delay_secs)

    print("Hosted managed-local Claude stress run passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
