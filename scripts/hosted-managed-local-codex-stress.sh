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
PROMPT_PREFIX="lh-hosted-codex-stress"
PROJECT_NAME="hosted-managed-local-codex-stress"
DISPLAY_NAME="Hosted Managed Local Codex Stress"
LOOP_MODE="assist"
CHAT_TIMEOUT_SECS="30"
VERIFY_TIMEOUT_SECS="30"
MACHINE_NAME="${MACHINE_NAME:-}"
if [[ -z "$MACHINE_NAME" && -f "$HOME/.claude/longhouse-machine-name" ]]; then
  MACHINE_NAME="$(tr -d '\r\n' < "$HOME/.claude/longhouse-machine-name")"
fi

usage() {
  cat <<'EOF'
Usage:
  scripts/hosted-managed-local-codex-stress.sh [options]

Launch a real hosted managed-local Codex session on this device, then send
repeated simple one-line prompts through the real `/api/sessions/{id}/chat`
route and verify each turn against the live tmux pane ground truth.

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
  --chat-timeout-secs <n>   Max wait for `/chat` SSE before failing (default: 30)
  --verify-timeout-secs <n> Poll timeout for tmux-pane verification (default: 30)
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
  "$(lh_hosted_create_device_token "$LH_STRESS_ACCESS_TOKEN" "$API_URL" "hosted-codex-stress-${INSTANCE_SUBDOMAIN}-${RANDOM}")"

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
export LH_STRESS_MACHINE_NAME="$MACHINE_NAME"

cd "$ROOT_DIR"
uv run --project server python -u <<'PY'
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from typing import Iterator

import httpx
from zerg.services.managed_local_control import validate_managed_local_chat_done_payload
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_SERVER_LABEL
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str


@dataclass(frozen=True)
class ChatRouteResult:
    status_code: int
    saw_assistant_delta: bool = False
    saw_done: bool = False
    stream_timed_out: bool = False
    done_payload: dict[str, object] | None = None
    error: str | None = None


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


def _run_local_shell(command: str, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=True,
        executable="/bin/zsh",
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _parse_tmux_tmpdir_from_attach_command(attach_command: str) -> str | None:
    parts = shlex.split(attach_command)
    if len(parts) < 3 or parts[0] != "zsh" or parts[1] != "-lc":
        return None
    inner = parts[2]
    match = re.search(r"(^|;)\s*export TMUX_TMPDIR=(.+?)(?=;|$)", inner)
    if match is None:
        return None
    assignment = shlex.split(f"TMUX_TMPDIR={match.group(2)}")
    if len(assignment) != 1 or not assignment[0].startswith("TMUX_TMPDIR="):
        return None
    value = assignment[0].split("=", 1)[1].strip()
    return value or None


def _tmux_wrapped_command(*, tmux_tmpdir: str | None, tmux_command: str) -> str:
    inner = [build_managed_local_shell_prelude(tmux_tmpdir=tmux_tmpdir), tmux_command]
    return f"zsh -lc {shlex.quote('; '.join(inner))}"


def _run_tmux_action(*, session_name: str, tmux_tmpdir: str | None, args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    tmux_command = " ".join(
        [f"tmux -L {shlex.quote(MANAGED_LOCAL_TMUX_SERVER_LABEL)}"]
        + [shlex.quote(part) for part in args]
    )
    return _run_local_shell(_tmux_wrapped_command(tmux_tmpdir=tmux_tmpdir, tmux_command=tmux_command), timeout=timeout)


def _has_tmux_session(*, session_name: str, tmux_tmpdir: str | None) -> bool:
    completed = _run_local_shell(
        build_tmux_has_session_command(session_name=session_name, tmux_tmpdir=tmux_tmpdir),
        timeout=10.0,
    )
    return completed.returncode == 0


def _capture_tmux_pane(*, session_name: str, tmux_tmpdir: str | None, lines: int = 220) -> str:
    completed = _run_local_shell(
        build_tmux_capture_command(session_name=session_name, lines=lines, tmux_tmpdir=tmux_tmpdir),
        timeout=15.0,
    )
    return (completed.stdout or completed.stderr or "").strip()


def _pane_is_ready(pane: str) -> bool:
    blocked_markers = (
        "Starting MCP servers",
        "Loading conversation history",
    )
    return "OpenAI Codex" in pane and not any(marker in pane for marker in blocked_markers)


def _pane_contains_exact_token_line(pane: str, token: str) -> bool:
    for line in pane.splitlines():
        stripped = line.strip()
        if stripped == token:
            return True
        normalized = re.sub(r"^[^\w-]+\s*", "", stripped)
        if normalized == token:
            return True
    return False


def _wait_for_ready_prompt(*, session_name: str, tmux_tmpdir: str | None, timeout_secs: float) -> str:
    deadline = time.monotonic() + timeout_secs
    last_pane = ""
    while time.monotonic() < deadline:
        pane = _capture_tmux_pane(session_name=session_name, tmux_tmpdir=tmux_tmpdir)
        last_pane = pane
        if _pane_is_ready(pane):
            return pane
        time.sleep(1.0)
    raise RuntimeError(
        "Codex never reached a usable idle prompt.\n"
        f"Last pane:\n{last_pane[-4000:]}"
    )


def _wait_for_turn_in_tmux(
    *,
    session_name: str,
    tmux_tmpdir: str | None,
    prompt: str,
    token: str,
    timeout_secs: float,
) -> tuple[str, bool, bool]:
    deadline = time.monotonic() + timeout_secs
    last_pane = ""
    while time.monotonic() < deadline:
        pane = _capture_tmux_pane(session_name=session_name, tmux_tmpdir=tmux_tmpdir)
        last_pane = pane
        prompt_seen = prompt in pane
        token_seen = _pane_contains_exact_token_line(pane, token)
        if prompt_seen and token_seen and _pane_is_ready(pane):
            return pane, prompt_seen, token_seen
        time.sleep(1.0)
    raise RuntimeError(
        f"Timed out waiting for Codex tmux confirmation for token {token}.\n"
        f"Last pane:\n{last_pane[-4000:]}"
    )


def _send_chat_via_api(
    *,
    api_url: str,
    access_token: str,
    session_id: str,
    prompt: str,
    timeout_secs: float,
) -> ChatRouteResult:
    if shutil.which("curl") is None:
        return ChatRouteResult(status_code=0, error="curl is required for hosted managed-local SSE verification")
    status_code = 0
    saw_assistant_delta = False
    saw_done = False
    done_payload: dict[str, object] | None = None
    prompt_body = json.dumps({"message": prompt})
    curl_timeout = max(5.0, timeout_secs)
    curl_deadline = int(curl_timeout + 5)
    try:
        with tempfile.NamedTemporaryFile() as body_file:
            curl = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-N",
                    "--connect-timeout",
                    "20",
                    "--max-time",
                    str(int(curl_timeout)),
                    "-X",
                    "POST",
                    "-H",
                    "Accept: text/event-stream",
                    "-H",
                    "Content-Type: application/json",
                    "-o",
                    body_file.name,
                    "-w",
                    "\\n%{http_code}\\n",
                    f"{api_url.rstrip('/')}/api/sessions/{session_id}/chat?token={access_token}",
                    "--data-binary",
                    prompt_body,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=curl_deadline,
            )
            status_line = (curl.stdout or "").strip().splitlines()
            if status_line:
                candidate = status_line[-1].strip()
                if candidate.isdigit():
                    status_code = int(candidate)
            body_text = Path(body_file.name).read_text(encoding="utf-8", errors="replace")

        for event in parse_sse_lines(body_text.replace("\r\n", "\n").splitlines(keepends=True)):
            if event.event == "assistant_delta":
                saw_assistant_delta = True
            if event.event == "error":
                try:
                    parsed = json.loads(event.data)
                    error = str(parsed.get("error") or event.data)
                except json.JSONDecodeError:
                    error = event.data
                return ChatRouteResult(
                    status_code=status_code,
                    saw_assistant_delta=saw_assistant_delta,
                    saw_done=saw_done,
                    stream_timed_out=curl.returncode == 28,
                    error=error,
                )
            if event.event == "done":
                saw_done = True
                try:
                    done_payload = json.loads(event.data)
                except json.JSONDecodeError:
                    return ChatRouteResult(
                        status_code=status_code,
                        saw_assistant_delta=saw_assistant_delta,
                        saw_done=True,
                        error=f"Malformed done payload: {event.data[:200]}",
                    )
                break

        if status_code != 200:
            preview = body_text[:400]
            return ChatRouteResult(
                status_code=status_code,
                saw_assistant_delta=saw_assistant_delta,
                saw_done=saw_done,
                stream_timed_out=curl.returncode == 28,
                error=f"chat status={status_code} body={preview}",
            )
        if curl.returncode not in (0, 28):
            detail = (curl.stderr or "").strip() or f"curl exit code {curl.returncode}"
            return ChatRouteResult(
                status_code=status_code,
                saw_assistant_delta=saw_assistant_delta,
                saw_done=saw_done,
                error=detail,
            )
        if not saw_done:
            return ChatRouteResult(
                status_code=status_code,
                saw_assistant_delta=saw_assistant_delta,
                saw_done=False,
                stream_timed_out=curl.returncode == 28,
                error="Managed-local chat stream never produced a done event",
            )
    except Exception as exc:
        return ChatRouteResult(status_code=status_code, error=f"{type(exc).__name__}: {exc}")

    return ChatRouteResult(
        status_code=status_code,
        saw_assistant_delta=saw_assistant_delta,
        saw_done=saw_done,
        done_payload=done_payload,
    )


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

    if shutil.which("tmux") is None:
        raise RuntimeError("tmux is required for hosted managed-local Codex tmux verification")

    timeout = httpx.Timeout(connect=20.0, read=chat_timeout_secs, write=20.0, pool=20.0)
    with httpx.Client(timeout=timeout) as client:
        health = client.get(f"{api_url.rstrip('/')}/api/health")
        if health.status_code != 200:
            raise RuntimeError(f"health status={health.status_code} body={health.text[:400]}")

        launch_body: dict[str, object] = {
            "cwd": cwd,
            "provider": "codex",
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
        session_name = str(launch_payload["managed_session_name"])
        attach_command = str(launch_payload["attach_command"])
        tmux_tmpdir = _parse_tmux_tmpdir_from_attach_command(attach_command)
        print(f"Tenant: {subdomain}")
        print(f"Session: {session_id}")
        print(f"Tmux session: {session_name}")
        if tmux_tmpdir:
            print(f"TMUX_TMPDIR: {tmux_tmpdir}")
        print(f"Attach: {attach_command}")

        session_deadline = time.monotonic() + verify_timeout_secs
        while time.monotonic() < session_deadline:
            if _has_tmux_session(session_name=session_name, tmux_tmpdir=tmux_tmpdir):
                break
            time.sleep(1.0)
        else:
            raise RuntimeError(f"tmux session {session_name!r} never appeared")

        ready_pane = _wait_for_ready_prompt(
            session_name=session_name,
            tmux_tmpdir=tmux_tmpdir,
            timeout_secs=verify_timeout_secs,
        )
        print("Ready: yes")
        if ready_pane:
            print(f"Initial pane tail:\n{ready_pane[-600:]}")

        prompts = build_prompts(count=turn_count, prefix=prompt_prefix)

        for idx, (prompt, token) in enumerate(prompts, start=1):
            chat_result = _send_chat_via_api(
                api_url=api_url,
                access_token=access_token,
                session_id=session_id,
                prompt=prompt,
                timeout_secs=chat_timeout_secs,
            )
            tmux_error = None
            prompt_seen = False
            token_seen = False
            pane_tail = ""
            if chat_result.status_code == 200:
                try:
                    pane, prompt_seen, token_seen = _wait_for_turn_in_tmux(
                        session_name=session_name,
                        tmux_tmpdir=tmux_tmpdir,
                        prompt=prompt,
                        token=token,
                        timeout_secs=verify_timeout_secs,
                    )
                    pane_tail = pane[-800:]
                except Exception as exc:
                    tmux_error = f"{type(exc).__name__}: {exc}"
                    pane_tail = _capture_tmux_pane(session_name=session_name, tmux_tmpdir=tmux_tmpdir)[-1200:]

            done_payload_error = validate_managed_local_chat_done_payload(
                session_id=session_id,
                done_payload=chat_result.done_payload,
            )

            ok = (
                chat_result.status_code == 200
                and chat_result.error is None
                and done_payload_error is None
                and tmux_error is None
                and prompt_seen
                and token_seen
            )
            label = "ok" if ok else "fail"
            print(
                f"[{idx}/{len(prompts)}] {label} status={chat_result.status_code} prompt={prompt!r} "
                f"token={token} tmux_prompt_seen={int(prompt_seen)} tmux_token_seen={int(token_seen)} "
                f"api_done={int(chat_result.saw_done)} api_delta={int(chat_result.saw_assistant_delta)} "
                f"api_timed_out={int(chat_result.stream_timed_out)}"
            )
            if chat_result.error:
                print(f"  api_error: {chat_result.error}")
            if chat_result.done_payload is not None:
                print(f"  done_payload: {json.dumps(chat_result.done_payload, sort_keys=True)}")
            if done_payload_error:
                print(f"  done_payload_error: {done_payload_error}")
            if tmux_error:
                print(f"  tmux_error: {tmux_error}")
            if pane_tail:
                print(f"  pane_tail:\n{pane_tail}")
            if not ok:
                return 1
            if delay_secs:
                time.sleep(delay_secs)

    print("Hosted managed-local Codex stress run passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
