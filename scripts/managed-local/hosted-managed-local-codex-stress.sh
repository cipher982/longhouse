#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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
DURABILITY_TIMEOUT_SECS="20"
MACHINE_NAME="${MACHINE_NAME:-}"
if [[ -z "$MACHINE_NAME" && -f "$HOME/.claude/longhouse-machine-name" ]]; then
  MACHINE_NAME="$(tr -d '\r\n' < "$HOME/.claude/longhouse-machine-name")"
fi

usage() {
  cat <<'EOF'
Usage:
  scripts/hosted-managed-local-codex-stress.sh [options]

Launch a real hosted managed-local Codex session on this device, then send
repeated simple one-line prompts through the real `/api/sessions/{id}/send-live`
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
  --chat-timeout-secs <n>   Max wait for `/send-live` HTTP response (default: 30)
  --verify-timeout-secs <n> Poll timeout for tmux-pane verification (default: 30)
  --durability-timeout-secs <n>
                            Max follow-up wait for transcript durability when sync_status=pending (default: 20)
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
    --durability-timeout-secs)
      [[ -n "${2:-}" ]] || { echo "--durability-timeout-secs requires a value" >&2; exit 1; }
      DURABILITY_TIMEOUT_SECS="$2"
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
export LH_STRESS_DURABILITY_TIMEOUT_SECS="$DURABILITY_TIMEOUT_SECS"
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

import httpx
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_SERVER_LABEL
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command


@dataclass(frozen=True)
class ChatRouteResult:
    status_code: int
    accepted: bool = False
    sync_status: str | None = None
    ack_payload: dict[str, object] | None = None
    request_id: str | None = None
    dispatch_ms: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class DurabilityCheck:
    ok: bool
    attempts: int = 0
    total_events: int = 0
    prompt_seen: bool = False
    token_seen: bool = False
    error: str | None = None


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
    return _text_contains_exact_token_line(pane, token)


def _text_contains_exact_token_line(text: str, token: str) -> bool:
    for line in text.splitlines():
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


def _assess_send_live_ack(*, status_code: int, payload: object) -> ChatRouteResult:
    parsed = payload if isinstance(payload, dict) else None
    if status_code != 200:
        if parsed is not None:
            error = str(parsed.get("error") or parsed)[:400]
        else:
            error = str(payload)[:400]
        return ChatRouteResult(status_code=status_code, ack_payload=parsed, error=error)
    if parsed is None:
        return ChatRouteResult(
            status_code=status_code,
            error=f"Expected JSON ack object, got {type(payload).__name__}",
        )
    if not bool(parsed.get("accepted")):
        return ChatRouteResult(
            status_code=status_code,
            ack_payload=parsed,
            error=str(parsed.get("error") or parsed)[:400],
        )
    dispatch_ms_raw = parsed.get("dispatch_ms")
    dispatch_ms: float | None = None
    try:
        if dispatch_ms_raw is not None:
            dispatch_ms = float(dispatch_ms_raw)
    except (TypeError, ValueError):
        dispatch_ms = None
    request_id = str(parsed.get("request_id") or "").strip() or None
    return ChatRouteResult(
        status_code=status_code,
        accepted=True,
        sync_status="pending",
        ack_payload=parsed,
        request_id=request_id,
        dispatch_ms=dispatch_ms,
    )


def _fetch_session_events(
    *,
    client: httpx.Client,
    api_url: str,
    device_token: str,
    session_id: str,
) -> tuple[list[dict[str, object]], int]:
    response = client.get(
        f"{api_url.rstrip('/')}/api/agents/sessions/{session_id}/events",
        headers={"X-Agents-Token": device_token},
        params={"limit": 500, "branch_mode": "head"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"events status={response.status_code} body={response.text[:400]}")
    payload = response.json()
    events = payload.get("events")
    if not isinstance(events, list):
        raise RuntimeError(f"malformed events payload: {payload!r}")
    return events, _coerce_int(payload.get("total"), default=len(events))


def _events_contain_turn(events: Iterable[dict[str, object]], *, prompt: str, token: str) -> tuple[bool, bool]:
    prompt_seen = False
    token_seen = False
    for event in events:
        if not isinstance(event, dict):
            continue
        role = str(event.get("role") or "").strip().lower()
        content_text = str(event.get("content_text") or "")
        if role == "user" and content_text == prompt:
            prompt_seen = True
        if role == "assistant" and _text_contains_exact_token_line(content_text, token):
            token_seen = True
    return prompt_seen, token_seen


def _wait_for_transcript_durability(
    *,
    client: httpx.Client,
    api_url: str,
    device_token: str,
    session_id: str,
    prompt: str,
    token: str,
    timeout_secs: float,
    poll_interval_secs: float = 1.0,
) -> DurabilityCheck:
    deadline = time.monotonic() + timeout_secs
    attempts = 0
    last_total = 0
    last_prompt_seen = False
    last_token_seen = False
    last_error = ""

    while True:
        attempts += 1
        try:
            events, total_events = _fetch_session_events(
                client=client,
                api_url=api_url,
                device_token=device_token,
                session_id=session_id,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            last_total = total_events
            last_prompt_seen, last_token_seen = _events_contain_turn(events, prompt=prompt, token=token)
            if last_prompt_seen and last_token_seen:
                return DurabilityCheck(
                    ok=True,
                    attempts=attempts,
                    total_events=last_total,
                    prompt_seen=True,
                    token_seen=True,
                )
            last_error = (
                f"prompt_seen={int(last_prompt_seen)} token_seen={int(last_token_seen)} total_events={last_total}"
            )

        if time.monotonic() >= deadline:
            return DurabilityCheck(
                ok=False,
                attempts=attempts,
                total_events=last_total,
                prompt_seen=last_prompt_seen,
                token_seen=last_token_seen,
                error=(
                    "Transcript durability did not land before timeout "
                    f"(prompt={int(last_prompt_seen)} token={int(last_token_seen)} total_events={last_total}); "
                    f"last_check={last_error}"
                ),
            )
        time.sleep(poll_interval_secs)


def _send_live_via_api(
    *,
    api_url: str,
    access_token: str,
    session_id: str,
    prompt: str,
    timeout_secs: float,
) -> ChatRouteResult:
    if shutil.which("curl") is None:
        return ChatRouteResult(status_code=0, error="curl is required for hosted managed-local send-live verification")
    status_code = 0
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
                    "Content-Type: application/json",
                    "-o",
                    body_file.name,
                    "-w",
                    "\\n%{http_code}\\n",
                    f"{api_url.rstrip('/')}/api/sessions/{session_id}/send-live?token={access_token}",
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

        if status_code != 200:
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                parsed = {"raw": body_text[:400]}
            return _assess_send_live_ack(status_code=status_code, payload=parsed)
        if curl.returncode not in (0, 28):
            detail = (curl.stderr or "").strip() or f"curl exit code {curl.returncode}"
            return ChatRouteResult(status_code=status_code, error=detail)
        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError:
            parsed = {"raw": body_text[:400]}
        return _assess_send_live_ack(status_code=status_code, payload=parsed)
    except Exception as exc:
        return ChatRouteResult(status_code=status_code, error=f"{type(exc).__name__}: {exc}")


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
    durability_timeout_secs = float(os.environ["LH_STRESS_DURABILITY_TIMEOUT_SECS"])
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
            send_result = _send_live_via_api(
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
            durability_check: DurabilityCheck | None = None
            if send_result.status_code == 200 and send_result.accepted:
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

            if (
                send_result.accepted
                and send_result.sync_status == "pending"
                and send_result.error is None
                and tmux_error is None
                and prompt_seen
                and token_seen
            ):
                durability_check = _wait_for_transcript_durability(
                    client=client,
                    api_url=api_url,
                    device_token=device_token,
                    session_id=session_id,
                    prompt=prompt,
                    token=token,
                    timeout_secs=durability_timeout_secs,
                )

            ok = (
                send_result.status_code == 200
                and send_result.accepted
                and send_result.error is None
                and tmux_error is None
                and prompt_seen
                and token_seen
                and (durability_check is None or durability_check.ok)
            )
            label = "ok" if ok else "fail"
            print(
                f"[{idx}/{len(prompts)}] {label} status={send_result.status_code} prompt={prompt!r} "
                f"token={token} tmux_prompt_seen={int(prompt_seen)} tmux_token_seen={int(token_seen)} "
                f"api_accepted={int(send_result.accepted)} "
                f"dispatch_ms={send_result.dispatch_ms if send_result.dispatch_ms is not None else 'missing'} "
                f"sync_status={send_result.sync_status or 'missing'}"
            )
            if send_result.request_id:
                print(f"  request_id: {send_result.request_id}")
            if send_result.error:
                print(f"  api_error: {send_result.error}")
            if send_result.ack_payload is not None:
                print(f"  ack_payload: {json.dumps(send_result.ack_payload, sort_keys=True)}")
            if durability_check is not None:
                print(
                    "  durability_check: "
                    f"ok={int(durability_check.ok)} attempts={durability_check.attempts} "
                    f"prompt_seen={int(durability_check.prompt_seen)} token_seen={int(durability_check.token_seen)} "
                    f"total_events={durability_check.total_events}"
                )
                if durability_check.error:
                    print(f"  durability_error: {durability_check.error}")
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
