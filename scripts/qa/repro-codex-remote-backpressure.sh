#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: repro-codex-remote-backpressure.sh [--mode command|text] [--lines N] [--log-dir PATH] [--cwd PATH]

Launches `longhouse-engine codex-app-server-canary` against the local managed
Codex runtime over websocket, attaches a real remote TUI, and asks Codex to run
one high-volume shell command so the remote transport sees a burst of
command-output deltas.

Environment overrides:
  ENGINE                 longhouse-engine binary (default: longhouse-engine)
  CODEX_BIN              managed codex binary (default: ~/.longhouse/runtimes/codex/current/codex)
  MODEL                  optional model override passed to the canary
  MODE                   stress mode: command or text (default: command)
  LINES                  number of lines to request (default: 20000)
  EVENT_TIMEOUT_SECS     overall canary timeout (default: 180)
  REMOTE_TUI_GRACE_MS    post-launch TUI grace window (default: 5000)
  SUBSCRIBE_PHASE        second-client subscribe timing: preturn, postturn, or after_rollout
                        (default: postturn)
  EXPECTED_FAILURE_PATTERN
                        if set, treat a nonzero canary exit as success when the
                        JSONL log contains this literal substring
  LOG_DIR                directory for summary/log artifacts (default: temp dir)
  CWD                    workspace directory bound to the thread (default: temp dir)
  REAL_HOME              set to 1 to reuse the real HOME/CODEX_HOME instead of an isolated copy

Exit status:
  0  turn completed and remote TUI stayed alive through the stress run
  1  canary command failed or the remote TUI dropped before shutdown
EOF
}

ENGINE="${ENGINE:-longhouse-engine}"
CODEX_BIN="${CODEX_BIN:-$HOME/.longhouse/runtimes/codex/current/codex}"
MODE="${MODE:-command}"
LINES="${LINES:-20000}"
EVENT_TIMEOUT_SECS="${EVENT_TIMEOUT_SECS:-180}"
REMOTE_TUI_GRACE_MS="${REMOTE_TUI_GRACE_MS:-5000}"
SUBSCRIBE_PHASE="${SUBSCRIBE_PHASE:-postturn}"
EXPECTED_FAILURE_PATTERN="${EXPECTED_FAILURE_PATTERN:-}"
LOG_DIR="${LOG_DIR:-}"
CWD="${CWD:-}"
REAL_HOME="${REAL_HOME:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --lines)
      LINES="${2:-}"
      shift 2
      ;;
    --log-dir)
      LOG_DIR="${2:-}"
      shift 2
      ;;
    --cwd)
      CWD="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! command -v "$ENGINE" >/dev/null 2>&1; then
  echo "missing engine binary: $ENGINE" >&2
  exit 1
fi

if [[ ! -x "$CODEX_BIN" ]]; then
  echo "managed codex binary not found or not executable: $CODEX_BIN" >&2
  exit 1
fi

if [[ "$MODE" != "command" && "$MODE" != "text" ]]; then
  echo "unsupported mode: $MODE (expected command or text)" >&2
  exit 1
fi

if [[ -z "$LOG_DIR" ]]; then
  LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-codex-backpressure-logs.XXXXXX")"
else
  mkdir -p "$LOG_DIR"
fi

if [[ -z "$CWD" ]]; then
  CWD="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-codex-backpressure-cwd.XXXXXX")"
else
  mkdir -p "$CWD"
fi

SUMMARY_JSON="$LOG_DIR/summary.json"
JSONL_LOG="$LOG_DIR/canary.jsonl"
REMOTE_TUI_LOG="$LOG_DIR/remote-tui.log"

if [[ "$MODE" == "command" ]]; then
  PROMPT=$(cat <<EOF
You are running a websocket backpressure stress probe.

Do not ask follow-up questions. Use a shell command to print exactly ${LINES} lines
to stdout as fast as possible. Use this exact program body:

python3 -c 'for i in range(${LINES}): print(f"BURST {i:05d}")'

After the command finishes, reply with exactly STRESS_OK.
EOF
)
else
  PROMPT=$(cat <<EOF
You are running a websocket backpressure stress probe.

Do not use any tools. Reply directly with exactly ${LINES} lines. Each line must be
BURST 00000, BURST 00001, and so on in order, zero-padded to 5 digits. After
the numbered burst, end with one final line containing exactly STRESS_OK.
EOF
)
fi

cmd=(
  "$ENGINE" codex-app-server-canary
  --prompt "$PROMPT"
  --cwd "$CWD"
  --codex-bin "$CODEX_BIN"
  --app-server-transport websocket
  --spawn-remote-tui
  --approval-policy on-request
  --auto-approve
  --sandbox workspace-write
  --event-timeout-secs "$EVENT_TIMEOUT_SECS"
  --remote-tui-grace-ms "$REMOTE_TUI_GRACE_MS"
  --remote-tui-subscribe-phase "$SUBSCRIBE_PHASE"
  --remote-tui-log "$REMOTE_TUI_LOG"
  --log-jsonl "$JSONL_LOG"
  --json
)

if [[ -n "${MODEL:-}" ]]; then
  cmd+=(--model "$MODEL")
fi

if [[ "$REAL_HOME" == "1" ]]; then
  cmd+=(--real-home)
fi

echo "Managed Codex remote backpressure probe"
echo "  engine:   $ENGINE"
echo "  codex:    $CODEX_BIN"
echo "  mode:     $MODE"
echo "  cwd:      $CWD"
echo "  log_dir:  $LOG_DIR"
echo "  lines:    $LINES"
echo "  subscribe_phase: $SUBSCRIBE_PHASE"
echo ""

set +e
"${cmd[@]}" >"$SUMMARY_JSON"
status=$?
set -e

if [[ $status -ne 0 ]]; then
  if [[ -n "$EXPECTED_FAILURE_PATTERN" ]] && [[ -f "$JSONL_LOG" ]] && grep -Fq "$EXPECTED_FAILURE_PATTERN" "$JSONL_LOG"; then
    echo "Result"
    echo "  observed expected failure pattern: $EXPECTED_FAILURE_PATTERN"
    echo "summary:  $SUMMARY_JSON"
    echo "jsonl:    $JSONL_LOG"
    echo "remote:   $REMOTE_TUI_LOG"
    exit 0
  fi
  echo "canary command failed with exit $status" >&2
  echo "summary:  $SUMMARY_JSON" >&2
  echo "jsonl:    $JSONL_LOG" >&2
  echo "remote:   $REMOTE_TUI_LOG" >&2
  exit 1
fi

python3 - "$SUMMARY_JSON" "$JSONL_LOG" "$REMOTE_TUI_LOG" "$MODE" <<'PY'
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
jsonl_log = Path(sys.argv[2])
remote_tui_log = Path(sys.argv[3])
mode = sys.argv[4]
summary = json.loads(summary_path.read_text())

command_output_lines = 0
command_output_bytes = 0
for line in jsonl_log.read_text().splitlines():
    payload = json.loads(line)
    if payload.get("direction") != "server_message":
        continue
    message = payload.get("payload") or {}
    if message.get("method") != "item/completed":
        continue
    item = ((message.get("params") or {}).get("item") or {})
    if item.get("type") != "commandExecution":
        continue
    aggregated = item.get("aggregatedOutput")
    if isinstance(aggregated, str):
        command_output_bytes += len(aggregated)
        command_output_lines += len(aggregated.splitlines())

turn_status = summary.get("turn_status")
alive_after_grace = summary.get("remote_tui_alive_after_grace")
alive_before_shutdown = summary.get("remote_tui_alive_before_shutdown")
assistant_text = summary.get("assistant_text", "")
received = summary.get("received_notifications", {})

print("Summary")
print(f"  turn_status: {turn_status}")
print(f"  remote_tui_alive_after_grace: {alive_after_grace}")
print(f"  remote_tui_alive_before_shutdown: {alive_before_shutdown}")
print(f"  assistant_text: {assistant_text!r}")
print(f"  command_output_delta_count: {received.get('item/commandExecution/outputDelta', 0)}")
print(f"  command_aggregated_output_lines: {command_output_lines}")
print(f"  command_aggregated_output_bytes: {command_output_bytes}")
print(f"  agent_message_delta_count: {received.get('item/agentMessage/delta', 0)}")
print(f"  jsonl_log: {jsonl_log}")
print(f"  remote_tui_log: {remote_tui_log}")

failed = (
    turn_status != "completed"
    or alive_after_grace is not True
    or alive_before_shutdown is not True
)

if failed:
    print("Result")
    print("  remote websocket path is still unstable under this stress run", file=sys.stderr)
    sys.exit(1)

print("Result")
if mode == "command" and command_output_lines == 0:
    print("  remote websocket path survived, but this run did not produce command output")
elif mode == "text" and received.get("item/agentMessage/delta", 0) == 0:
    print("  remote websocket path survived, but this run did not produce streamed assistant text")
else:
    print("  remote websocket path survived this stress run")
PY
