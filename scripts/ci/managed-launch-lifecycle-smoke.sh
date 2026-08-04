#!/usr/bin/env bash
# Drive a real `longhouse <provider>` launch against a REAL Runtime Host.
#
# Why this exists
# ---------------
# `longhouse cursor` was dead for 4 days 21 hours behind green CI. The only job
# that ran any `longhouse <provider>` entrypoint was
# scripts/ci/native-installer-smoke.sh, whose fake Runtime Host returns a
# coordination token for any request carrying a session_id. A server-side
# provider gate is structurally invisible to it: the fake cannot refuse, so it
# cannot catch a refusal.
#
# That smoke deliberately traps python/uv to prove the native device path needs
# no Python, so a real server cannot live inside it. This is the separate lane:
# a real FastAPI Runtime Host with a real SQLite database, real device-token
# auth, real request validation, and real token policy -- the four things a
# hand-written fake reimplements badly, and exactly where the outage lived.
#
# Provider binaries stay scripted. The gap that mattered was the server.
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
UV_BIN="$(command -v uv)"
PYTHON_BIN="$(command -v python3)"
# Codex's per-session Unix socket must fit the platform SUN_LEN limit. Keep the
# smoke root short so the test exercises lifecycle behavior rather than the
# host's unusually long default macOS TMPDIR prefix.
TEST_ROOT="$(mktemp -d /tmp/lh.XXXXXX)"
HOME_DIR="$TEST_ROOT/home"
BIN_DIR="$TEST_ROOT/bin"
SERVER_LOG="$TEST_ROOT/server.log"
SERVER_PID=""
ENGINE_LOG="$TEST_ROOT/engine.log"
ENGINE_PID=""
CLAUDE_CONTROL_PID=""
CURSOR_CONTROL_PID=""
PORT="${LONGHOUSE_LIFECYCLE_SMOKE_PORT:-0}"

stop_process_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    stop_process_tree "$child"
  done
  kill "$pid" 2>/dev/null || true
  sleep 0.1
  kill -KILL "$pid" 2>/dev/null || true
}

stop_and_reap() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  # Provider facades and the Machine Agent can stay in their control loop when
  # the Runtime Host has already gone away. Recurse first, then force the
  # exact test process after the short grace period in stop_process_tree;
  # waiting on a soft kill alone can leave the EXIT trap hung indefinitely.
  stop_process_tree "$pid"
  wait "$pid" 2>/dev/null || true
}

stop_processes_for_test_root() {
  local pid
  for pid in $(ps -axo pid=,command= | grep -F "$TEST_ROOT" | awk '{print $1}' || true); do
    [[ "$pid" == "$$" ]] && continue
    kill -KILL "$pid" 2>/dev/null || true
  done
}

cleanup() {
  if [[ -n "$CURSOR_CONTROL_PID" ]]; then
    stop_and_reap "$CURSOR_CONTROL_PID"
  fi
  if [[ -n "$CLAUDE_CONTROL_PID" ]]; then
    stop_and_reap "$CLAUDE_CONTROL_PID"
  fi
  if [[ -n "$ENGINE_PID" ]]; then
    stop_and_reap "$ENGINE_PID"
  fi
  if [[ -n "$SERVER_PID" ]]; then
    stop_process_tree "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  stop_processes_for_test_root
  if [[ "${LONGHOUSE_KEEP_LIFECYCLE_SMOKE_ROOT:-0}" == "1" ]]; then
    echo "preserved lifecycle smoke root: $TEST_ROOT" >&2
  else
    rm -rf "$TEST_ROOT"
  fi
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  echo "--- server log tail ---" >&2
  tail -40 "$SERVER_LOG" >&2 || true
  exit 1
}

mkdir -p "$HOME_DIR" "$BIN_DIR"

# ---------------------------------------------------------------------------
# Build the real facade + engine pair
# ---------------------------------------------------------------------------
python3 "$ROOT_DIR/scripts/build/generate_build_identity.py" >/dev/null
cargo build --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci \
  --bin longhouse --bin longhouse-engine >/dev/null
cp "$ROOT_DIR/engine/target/ci/longhouse" "$BIN_DIR/longhouse"
cp "$ROOT_DIR/engine/target/ci/longhouse-engine" "$BIN_DIR/longhouse-engine"

# ---------------------------------------------------------------------------
# Start a real Runtime Host
# ---------------------------------------------------------------------------
# Picking an ephemeral port by bind-then-close leaves a window where another
# process can take it before the Runtime Host binds, which is a sporadic CI
# failure rather than a real one. Try a few times instead of assuming the race
# does not happen.
start_runtime_host() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if [[ "${LONGHOUSE_LIFECYCLE_SMOKE_PORT:-0}" != "0" ]]; then
      PORT="$LONGHOUSE_LIFECYCLE_SMOKE_PORT"
      for _ in $(seq 1 1200); do
        if "$PYTHON_BIN" -c 'import socket, sys; s=socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()' "$PORT" >/dev/null 2>&1; then
          break
        fi
        sleep 0.1
      done
    else
      PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    fi
    BASE_URL="http://127.0.0.1:$PORT"
    (
      cd "$ROOT_DIR/server"
      # Deliberately NOT TESTING=1: that forces live_catalog_enabled() false,
      # which short-circuits the coordination-token endpoint at its 503 guard
      # before the provider check and leaves the live store unused. A
      # counterpart that refuses for the wrong reason is only marginally better
      # than one that never refuses.
      AUTH_DISABLED=1 \
      LLM_DISABLED=1 \
      LOG_LEVEL="${LONGHOUSE_LIFECYCLE_SMOKE_LOG_LEVEL:-WARNING}" \
      DATABASE_URL="sqlite:///$TEST_ROOT/longhouse.db" \
      JWT_SECRET="lifecycle-smoke-jwt-secret" \
      FERNET_SECRET="$FERNET" \
      INTERNAL_API_SECRET="lifecycle-smoke-internal-secret" \
      "$UV_BIN" run python -m zerg.cli.main serve --host 127.0.0.1 --port "$PORT" >"$SERVER_LOG" 2>&1
    ) &
    SERVER_PID=$!

    local _
    for _ in $(seq 1 120); do
      if curl -fsS -o /dev/null "$BASE_URL/api/health" 2>/dev/null; then return 0; fi
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
      sleep 0.5
    done

    stop_process_tree "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
    if [[ "${LONGHOUSE_LIFECYCLE_SMOKE_PORT:-0}" != "0" ]]; then
      echo "runtime host did not come up on port $PORT; retrying the same port" >&2
    else
      echo "runtime host did not come up on port $PORT; retrying on a new port" >&2
    fi
  done
  return 1
}

FERNET="$(cd "$ROOT_DIR/server" && "$UV_BIN" run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

start_runtime_host || fail "Runtime Host never became healthy"
echo "runtime host up on $BASE_URL"

DEVICE_ID="11111111-1111-4111-8111-111111111111"
DEVICE_TOKEN="$(curl -fsS -X POST "$BASE_URL/api/devices/tokens" \
  -H 'content-type: application/json' \
  -d '{"name":"lifecycle-smoke","device_id":"11111111-1111-4111-8111-111111111111"}' \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])')"
[[ "$DEVICE_TOKEN" == zdt_* ]] || fail "expected a real device token, got ${DEVICE_TOKEN:0:8}"

launch_payload() {
  local provider="$1" session_id="$2"
  python3 - "$provider" "$session_id" <<'PY'
import json, sys
provider, session_id = sys.argv[1], sys.argv[2]
payload = {
    "cwd": "/tmp",
    "provider": provider,
    "loop_mode": "assist",
    "machine_name": "lifecycle-smoke-host",
    "permission_mode": "bypass",
}
if session_id:
    payload["session_id"] = session_id
print(json.dumps(payload))
PY
}

register() {
  local provider="$1" session_id="${2:-}"
  local response code body deadline=$((SECONDS + 15))
  while ((SECONDS < deadline)); do
    response="$(curl -sS -X POST "$BASE_URL/api/sessions/managed-local/this-device" \
      -H "X-Agents-Token: $DEVICE_TOKEN" \
      -H 'content-type: application/json' \
      -d "$(launch_payload "$provider" "$session_id")" \
      -w $'\n%{http_code}')"
    code="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [[ "$code" == "200" ]]; then
      printf '%s' "$body"
      return 0
    fi
    if [[ "$code" == "503" && "$body" == *"catalogd is unavailable"* ]]; then
      sleep 0.25
      continue
    fi
    printf '%s\n' "$body" >&2
    return 1
  done
  printf '%s\n' "$body" >&2
  return 1
}

json_field() {
  python3 -c 'import sys, json; d=json.load(sys.stdin); v=d.get(sys.argv[1]); print("" if v is None else v)' "$1"
}

launch_attempt_state() {
  sqlite3 "$TEST_ROOT/longhouse-live.db" \
    "SELECT state FROM live_session_launch_attempts WHERE session_id = '$1' ORDER BY id DESC LIMIT 1;"
}

latest_launch_session_id() {
  sqlite3 "$TEST_ROOT/longhouse-live.db" \
    "SELECT session_id FROM live_session_launch_attempts ORDER BY id DESC LIMIT 1;"
}

runtime_terminal_state() {
  sqlite3 "$TEST_ROOT/longhouse-live.db" \
    "SELECT terminal_state FROM live_runtime_state WHERE session_id = '$1' ORDER BY updated_at DESC LIMIT 1;"
}

wait_for_value() {
  local description="$1" expected="$2" timeout_secs="$3"
  shift 3
  local deadline=$((SECONDS + timeout_secs)) value=""
  while ((SECONDS < deadline)); do
    value="$("$@" 2>/dev/null || true)"
    if [[ "$value" == "$expected" ]]; then
      return 0
    fi
    sleep 0.2
  done
  fail "$description stayed '${value:-empty}', expected '$expected'"
}

wait_for_launch_terminal_state() {
  local session_id="$1" timeout_secs="$2"
  local deadline=$((SECONDS + timeout_secs)) value=""
  while ((SECONDS < deadline)); do
    value="$(launch_attempt_state "$session_id" 2>/dev/null || true)"
    case "$value" in
      adopted|failed|abandoned)
        printf '%s' "$value"
        return 0
        ;;
    esac
    sleep 0.2
  done
  fail "Runtime Host launch outcome for $session_id stayed '${value:-empty}' without a terminal state"
}

retry_file_count() {
  local directory="$1"
  find "$directory" -maxdepth 1 -name '*.json' -type f 2>/dev/null | wc -l | tr -d ' '
}

wait_for_retry_files_at_least() {
  local description="$1" expected="$2" timeout_secs="$3" directory="$4"
  local deadline=$((SECONDS + timeout_secs)) value=0
  while ((SECONDS < deadline)); do
    value="$(retry_file_count "$directory")"
    if ((value >= expected)); then
      return 0
    fi
    sleep 0.2
  done
  fail "$description stayed at '$value', expected at least '$expected'"
}

send_live() {
  local session_id="$1" message="$2"
  local deadline=$((SECONDS + 30)) response code body
  while ((SECONDS < deadline)); do
    response="$(curl -sS --max-time 40 -w $'\n%{http_code}' -X POST \
      "$BASE_URL/api/agents/sessions/$session_id/send-live" \
      -H "X-Agents-Token: $DEVICE_TOKEN" \
      -H 'content-type: application/json' \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"message":sys.argv[1]}))' "$message")")"
    code="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [[ "$code" == "200" ]]; then
      printf '%s' "$body"
      return 0
    fi
    if [[ "$body" == *"Managed control channel is not connected or does not advertise this capability"* ]]; then
      sleep 0.25
      continue
    fi
    if [[ "$code" != "409" || "$body" != *"does not have a live Longhouse control channel"* ]]; then
      printf '%s\n' "$body" >&2
      return 1
    fi
    sleep 0.25
  done
  printf '%s\n' "$body" >&2
  return 1
}

post_live_action() {
  local session_id="$1" action="$2"
  local deadline=$((SECONDS + 30)) response code body
  while ((SECONDS < deadline)); do
    response="$(curl -sS --max-time 40 -w $'\n%{http_code}' -X POST \
      "$BASE_URL/api/agents/sessions/$session_id/$action-live" \
      -H "X-Agents-Token: $DEVICE_TOKEN")"
    code="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [[ "$code" == "200" ]]; then
      printf '%s' "$body"
      return 0
    fi
    if [[ "$body" == *"Managed control channel is not connected or does not advertise this capability"* ]]; then
      sleep 0.25
      continue
    fi
    if [[ "$code" != "409" || "$body" != *"does not have a live Longhouse control channel"* ]]; then
      printf '%s\n' "$body" >&2
      return 1
    fi
    sleep 0.25
  done
  printf '%s\n' "$body" >&2
  return 1
}

# Keep provider launches bounded and use the shared PTY harness. The harness
# completes from child status rather than waiting for macOS PTY EOF, which can
# remain unreadable after the child has already been reaped.
run_launch_bounded() {
  local out_file="$1" timeout_secs="$2"
  shift 2
  python3 "$ROOT_DIR/scripts/ci/run-in-pty.py" --timeout "$timeout_secs" "$@" \
    >"$out_file" 2>&1
}

# ---------------------------------------------------------------------------
# 1. Registration is accepted, and the server issues coordination authority
#    for a provider whose contract declares it.
#
#    This is the exact assertion the fake Runtime Host could never make: it
#    answered every request identically, so the five days cursor could not
#    launch looked the same as the days it could.
# ---------------------------------------------------------------------------
for provider in cursor claude codex opencode; do
  response="$(register "$provider")" || fail "$provider registration was rejected"
  token="$(printf '%s' "$response" | json_field coordination_token)"
  [[ -n "$token" ]] || fail "$provider: Runtime Host issued no coordination token; every managed launcher hard-bails on this"
  run_id="$(printf '%s' "$response" | json_field run_id)"
  [[ -n "$run_id" ]] || fail "$provider: registration returned no run identity"
  echo "ok: $provider registration issues coordination authority"
done

# ---------------------------------------------------------------------------
# 2. The server can REFUSE. A counterpart that only ever says yes cannot prove
#    a gate exists.
# ---------------------------------------------------------------------------
agy_response="$(register antigravity)" || fail "antigravity registration failed outright"
agy_token="$(printf '%s' "$agy_response" | json_field coordination_token)"
[[ -z "$agy_token" ]] || fail "antigravity is Shadow-only and must not receive coordination authority"
echo "ok: Shadow-only provider is refused coordination authority"

# ---------------------------------------------------------------------------
# 3. A real `longhouse cursor` launch against the real server, under a real
#    PTY, with a scripted provider binary.
# ---------------------------------------------------------------------------
cat > "$BIN_DIR/cursor-agent" <<'PY'
#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tty
import uuid

conversation_id = "00000000-0000-0000-0000-000000000001"

if sys.argv[1:2] == ["create-chat"]:
    if os.environ.get("LONGHOUSE_FAKE_CURSOR_CREATE_FAIL") == "1":
        print("scripted create-chat failure", file=sys.stderr)
        raise SystemExit(9)
    print(conversation_id)
    raise SystemExit(0)

print("CURSOR_LIFECYCLE_PTY_OK", flush=True)
if os.environ.get("LONGHOUSE_FAKE_CURSOR_CONTROL") != "1":
    import time

    time.sleep(1)
    raise SystemExit(int(os.environ.get("LONGHOUSE_FAKE_CURSOR_EXIT", "0")))


def lifecycle(event, generation_id=None):
    payload = {
        "conversation_id": conversation_id,
        "cwd": os.getcwd(),
    }
    if generation_id:
        payload["generation_id"] = generation_id
    subprocess.run(
        ["longhouse-engine", "cursor-lifecycle-hook", event],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.DEVNULL,
        check=True,
    )


lifecycle("sessionStart")
tty.setraw(sys.stdin.fileno())
generation_id = None
buffer = bytearray()
while True:
    value = os.read(sys.stdin.fileno(), 1)
    if not value:
        break
    if value == b"\x03":
        lifecycle("stop", generation_id)
        continue
    if value == b"\x1b":
        continue
    if value in {b"\r", b"\n"}:
        generation_id = str(uuid.uuid4())
        lifecycle("beforeSubmitPrompt", generation_id)
        buffer.clear()
        continue
    buffer.extend(value)
PY
chmod 755 "$BIN_DIR/cursor-agent"

export HOME="$HOME_DIR"
export PATH="$BIN_DIR:/usr/bin:/bin:/usr/sbin:/sbin"
# The Codex protocol fake uses the same pinned Python environment as the real
# Runtime Host. Keep the provider executable self-contained on PATH while
# avoiding a second dependency installation inside this smoke.
cat > "$BIN_DIR/python3" <<EOF
#!/bin/sh
exec "$ROOT_DIR/server/.venv/bin/python" "\$@"
EOF
chmod 755 "$BIN_DIR/python3"

LONGHOUSE_DEVICE_TOKEN="$DEVICE_TOKEN" "$BIN_DIR/longhouse" auth --url "$BASE_URL" >/dev/null
[[ -f "$HOME_DIR/.longhouse/machine/device-token" ]] || fail "device token was not stored"

launch_out="$TEST_ROOT/cursor-launch.out"
set +e
run_launch_bounded "$launch_out" 90 \
  "$BIN_DIR/longhouse" cursor --verbose --cwd "$HOME_DIR" --cursor-bin "$BIN_DIR/cursor-agent"
launch_status=$?
set -e
if [[ "$launch_status" != "0" ]]; then
  echo "--- launch output ---" >&2
  cat "$launch_out" >&2
  fail "longhouse cursor exited $launch_status against a real Runtime Host"
fi
grep -q 'CURSOR_LIFECYCLE_PTY_OK' "$launch_out" || fail "the scripted provider never ran under the PTY"
cursor_session_id="$(sed -n 's/^Longhouse Cursor session: \([0-9a-f-]*\).*/\1/p' "$launch_out" | tail -1 | tr -d '\r')"
[[ -n "$cursor_session_id" ]] || fail "successful Cursor launch did not print its session identity"
cursor_launch_state="$(launch_attempt_state "$cursor_session_id")"
[[ "$cursor_launch_state" == "adopted" ]] \
  || fail "successful Cursor launch recorded $cursor_launch_state instead of adopted"
echo "ok: longhouse cursor launched against a real Runtime Host"

# Registration is not launch success. Fail the first irreversible provider
# action after registration and prove the launch attempt becomes failed rather
# than remaining a durable session that claims it launched.
failed_launch_out="$TEST_ROOT/cursor-start-failed.out"
set +e
LONGHOUSE_FAKE_CURSOR_CREATE_FAIL=1 run_launch_bounded "$failed_launch_out" 90 \
  "$BIN_DIR/longhouse" cursor --verbose --cwd "$HOME_DIR" --cursor-bin "$BIN_DIR/cursor-agent"
failed_launch_status=$?
set -e
[[ "$failed_launch_status" != "0" ]] || fail "scripted Cursor startup failure returned success"
failed_session_id="$(sed -n 's/^Longhouse Cursor session: \([0-9a-f-]*\).*/\1/p' "$failed_launch_out" | tail -1 | tr -d '\r')"
[[ -n "$failed_session_id" ]] || fail "failed Cursor launch did not print its registered session identity"
failed_launch_state="$(launch_attempt_state "$failed_session_id")"
[[ "$failed_launch_state" == "failed" ]] \
  || fail "failed Cursor startup recorded $failed_launch_state instead of failed"
echo "ok: provider startup failure aborts the registered launch"

# A provider image that cannot exec after `create-chat` must fail before the
# launcher records local readiness. This guards the exec-status pipe against
# silently treating EOF as a successful provider handoff; a non-executable
# file would fail earlier during binary resolution and would not exercise the
# final provider exec.
execfail_cursor_bin="$BIN_DIR/execfail-cursor-agent"
cat >"$execfail_cursor_bin" <<'PY'
#!/usr/bin/env python3
import sys

if sys.argv[1:2] == ["create-chat"]:
    print("00000000-0000-0000-0000-000000000001")
    raise SystemExit(0)
raise SystemExit(127)
PY
chmod 755 "$execfail_cursor_bin"
nonexec_launch_out="$TEST_ROOT/cursor-nonexec.out"
set +e
run_launch_bounded "$nonexec_launch_out" 90 \
  "$BIN_DIR/longhouse" cursor --verbose --cwd "$HOME_DIR" --cursor-bin "$execfail_cursor_bin"
nonexec_launch_status=$?
set -e
[[ "$nonexec_launch_status" != "0" ]] || fail "Cursor final provider exec failure returned success"
nonexec_retry_count=0
if [[ -d "$HOME_DIR/.longhouse/agent/managed-local/registration-retries" ]]; then
  nonexec_retry_count="$(find "$HOME_DIR/.longhouse/agent/managed-local/registration-retries" -name '*.json' -type f -print | wc -l | tr -d ' ')"
fi
[[ "$nonexec_retry_count" == "0" ]] \
  || fail "Cursor final provider exec failure left a durable registration retry intent"
echo "ok: provider exec failure aborts before local readiness"

# The resume leg of coordination authority, per provider, against the real
# server. This is the assertion the outage needed: `longhouse cursor --resume`
# calls this endpoint and hard-bails on anything but a token, and the endpoint
# gates on the same provider contract that was wrong.
for provider in cursor claude codex opencode; do
  session_id="$(register "$provider" | json_field session_id)"
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    "$BASE_URL/api/agents/sessions/$session_id/coordination-token" \
    -H "X-Agents-Token: $DEVICE_TOKEN")"
  [[ "$code" == "200" ]] \
    || fail "$provider resume-path coordination token returned $code; `longhouse $provider --resume` would refuse to start"
  echo "ok: $provider resume path issues coordination authority"
done

agy_resume="$(register antigravity | json_field session_id)"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/agents/sessions/$agy_resume/coordination-token" \
  -H "X-Agents-Token: $DEVICE_TOKEN")"
[[ "$code" == "409" ]] \
  || fail "Shadow-only provider resume returned $code, expected 409 from the provider gate"
echo "ok: resume path refuses a provider with no coordination tools (409 from the provider gate)"

# ---------------------------------------------------------------------------
# 3b. Claude launches for real too. Its prerequisite (`claude auth status
#     --json` reporting loggedIn) is cheap to script, so there is no reason to
#     leave the highest-traffic provider proven only at the HTTP layer.
#
#     Codex and OpenCode follow below with protocol-faithful local servers.
# ---------------------------------------------------------------------------
cat > "$BIN_DIR/claude" <<'PY'
#!/usr/bin/env python3
import datetime
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import uuid

if sys.argv[1:2] == ["auth"]:
    print('{"loggedIn": true}')
    raise SystemExit(0)

print("CLAUDE_LIFECYCLE_PTY_OK", flush=True)
if os.environ.get("LONGHOUSE_FAKE_CLAUDE_CONTROL") != "1":
    raise SystemExit(int(os.environ.get("LONGHOUSE_FAKE_CLAUDE_EXIT", "0")))

mcp_path = pathlib.Path(sys.argv[sys.argv.index("--mcp-config") + 1])
mcp = json.loads(mcp_path.read_text())["mcpServers"]["longhouse-channel"]
bridge_env = os.environ.copy()
bridge_env.update(mcp.get("env") or mcp.get("environment") or {})
bridge_env["LONGHOUSE_CHANNEL_PARENT_PID"] = str(os.getpid())
bridge = subprocess.Popen(
    [mcp["command"], *mcp.get("args", [])],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
    bufsize=1,
    env=bridge_env,
)
bridge.stdin.write(json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "lifecycle-fake", "version": "1"},
    },
}) + "\n")
bridge.stdin.flush()
json.loads(bridge.stdout.readline())
bridge.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
bridge.stdin.flush()

running = threading.Event()
running.set()
provider_session_id = os.environ["LONGHOUSE_PROVIDER_SESSION_ID"]
transcript = pathlib.Path.home() / ".claude/projects/lifecycle" / f"{provider_session_id}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)


def record_notifications():
    for line in bridge.stdout:
        message = json.loads(line)
        if message.get("method") != "notifications/claude/channel":
            continue
        content = message["params"]["content"]
        wrapped = (
            '<channel source="longhouse-channel" injected_by="longhouse">\n'
            + content
            + "\n</channel>"
        )
        event = {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": os.getcwd(),
            "sessionId": provider_session_id,
            "version": "lifecycle-fake",
            "type": "user",
            "message": {"role": "user", "content": wrapped},
            "uuid": str(uuid.uuid4()),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with transcript.open("a") as output:
            output.write(json.dumps(event) + "\n")
            output.flush()
            os.fsync(output.fileno())


threading.Thread(target=record_notifications, daemon=True).start()
signal.signal(signal.SIGINT, lambda *_: None)
signal.signal(signal.SIGTERM, lambda *_: running.clear())
while running.is_set():
    time.sleep(0.05)

bridge.stdin.close()
bridge.terminate()
bridge.wait(timeout=5)
PY
chmod 755 "$BIN_DIR/claude"

claude_out="$TEST_ROOT/claude-launch.out"
set +e
run_launch_bounded "$claude_out" 90 \
  "$BIN_DIR/longhouse" claude --cwd "$HOME_DIR" --claude-bin "$BIN_DIR/claude"
claude_status=$?
set -e
if [[ "$claude_status" != "0" ]]; then
  echo "--- claude launch output ---" >&2
  cat "$claude_out" >&2
  fail "longhouse claude exited $claude_status against a real Runtime Host"
fi
grep -q 'CLAUDE_LIFECYCLE_PTY_OK' "$claude_out" || fail "the scripted claude never ran under the PTY"
echo "ok: longhouse claude launched against a real Runtime Host"

# ---------------------------------------------------------------------------
# 3c. Codex launches through the actual native app-server bridge. This fake
#     implements the WebSocket JSON-RPC startup contract used by stock Codex:
#     initialize, initialized, and thread/start. It stays alive until the real
#     `longhouse codex stop` path tears down the bridge and provider process.
# ---------------------------------------------------------------------------
cat > "$BIN_DIR/codex" <<'PY'
#!/usr/bin/env python3
import json
import os
import sys

from websockets.sync.server import serve

if os.environ.get("LONGHOUSE_FAKE_CODEX_START_FAIL") == "1":
    print("scripted Codex app-server startup failure", file=sys.stderr, flush=True)
    raise SystemExit(9)

if "app-server" not in sys.argv[1:]:
    print("unexpected fake codex args: " + json.dumps(sys.argv[1:]), file=sys.stderr)
    raise SystemExit(2)


def handle(websocket):
    for raw in websocket:
        message = json.loads(raw)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialized":
            continue
        if method == "initialize":
            result = {
                "platformFamily": "unix",
                "platformOs": "macos",
                "userAgent": "longhouse-lifecycle-fake/1.0",
            }
        elif method == "thread/start":
            result = {"thread": {"id": "thr_lifecycle_fake"}}
        elif method == "turn/start":
            result = {
                "turn": {
                    "id": "turn_lifecycle_fake",
                    "status": "inProgress",
                    "items": [],
                }
            }
        elif method == "turn/interrupt":
            result = {}
        else:
            result = {}
        if request_id is not None:
            websocket.send(json.dumps({"id": request_id, "result": result}))
        if method == "turn/start":
            websocket.send(json.dumps({
                "method": "turn/started",
                "params": {
                    "threadId": "thr_lifecycle_fake",
                    "turn": result["turn"],
                },
            }))
        elif method == "turn/interrupt":
            websocket.send(json.dumps({
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_lifecycle_fake",
                    "turn": {
                        "id": "turn_lifecycle_fake",
                        "status": "interrupted",
                        "items": [],
                    },
                },
            }))


server = serve(handle, "127.0.0.1", 0)
port = server.socket.getsockname()[1]
print(f"listening on: ws://127.0.0.1:{port}", file=sys.stderr, flush=True)
server.serve_forever()
PY
chmod 755 "$BIN_DIR/codex"

codex_out="$TEST_ROOT/codex-launch.out"
"$BIN_DIR/longhouse" codex --no-attach --cwd "$HOME_DIR" --codex-bin "$BIN_DIR/codex" \
  >"$codex_out" 2>&1 || {
    cat "$codex_out" >&2
    fail "longhouse codex failed against its protocol-faithful app-server"
  }
grep -q 'Managed Codex ready' "$codex_out" || fail "Codex facade did not report readiness"
codex_session_id="$(latest_launch_session_id)"
[[ -n "$codex_session_id" ]] || fail "successful Codex launch did not record its session identity"
codex_launch_state="$(launch_attempt_state "$codex_session_id")"
[[ "$codex_launch_state" == "pending" || "$codex_launch_state" == "adopted" ]] \
  || fail "successful Codex launch recorded unexpected state $codex_launch_state"
"$BIN_DIR/longhouse" codex stop --session-id "$codex_session_id" \
  || fail "Codex launch could not be torn down through the real facade"
echo "ok: longhouse codex launched and stopped against a WebSocket app-server"

set +e
LONGHOUSE_FAKE_CODEX_START_FAIL=1 \
  "$BIN_DIR/longhouse" codex --no-attach --cwd "$HOME_DIR" --codex-bin "$BIN_DIR/codex" \
  >"$TEST_ROOT/codex-start-failed.out" 2>&1
codex_failed_status=$?
set -e
[[ "$codex_failed_status" != "0" ]] || fail "scripted Codex startup failure returned success"
codex_failed_session_id="$(latest_launch_session_id)"
[[ "$(launch_attempt_state "$codex_failed_session_id")" == "failed" ]] \
  || fail "failed Codex startup did not abort its launch transaction"
echo "ok: Codex app-server startup failure aborts the registered launch"

# ---------------------------------------------------------------------------
# 3d. OpenCode launches through its native HTTP bridge. The fake speaks the
#     authenticated health and session-creation surface used during startup.
# ---------------------------------------------------------------------------
cat > "$BIN_DIR/opencode" <<'PY'
#!/usr/bin/env python3
import base64
import http.server
import json
import os
import pathlib
import signal
import sys
import time
import uuid
from urllib.parse import urlparse

if os.environ.get("LONGHOUSE_FAKE_OPENCODE_START_FAIL") == "1":
    print("scripted OpenCode server startup failure", flush=True)
    raise SystemExit(9)

if not sys.argv[1:] or sys.argv[1] != "serve":
    print("unexpected fake opencode args: " + json.dumps(sys.argv[1:]), file=sys.stderr)
    raise SystemExit(2)

username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
session_id = os.environ.get("LONGHOUSE_MANAGED_SESSION_ID", "")


def write_runtime_phase():
    outbox = pathlib.Path.home() / ".longhouse/agent/runtime-events-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = {
        "runtime_key": f"opencode:{session_id}",
        "session_id": session_id,
        "provider": "opencode",
        "source": "lifecycle_protocol_fake",
        "kind": "phase_signal",
        "phase": "running",
        "occurred_at": now,
        "dedupe_key": f"opencode-lifecycle:{session_id}:{uuid.uuid4()}",
        "payload": {"managed_transport": "opencode_server_bridge"},
    }
    temporary = outbox / f".tmp.{uuid.uuid4()}"
    final = outbox / f"rte.{uuid.uuid4()}.json"
    temporary.write_text(json.dumps(payload))
    temporary.replace(final)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self):
        self.send_response(204)
        self.send_header("content-length", "0")
        self.end_headers()

    def authorized(self):
        value = base64.b64encode(f"{username}:{password}".encode()).decode()
        return self.headers.get("authorization") == f"Basic {value}"

    def do_GET(self):
        if not self.authorized():
            self.send_json({"error": "forbidden"}, 403)
        elif urlparse(self.path).path == "/global/health":
            self.send_json({"healthy": True})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.authorized():
            self.send_json({"error": "forbidden"}, 403)
        elif urlparse(self.path).path == "/session":
            self.send_json({"id": "ses_lifecycle_fake"})
        elif urlparse(self.path).path == "/session/ses_lifecycle_fake/prompt_async":
            length = int(self.headers.get("content-length") or "0")
            if length:
                self.rfile.read(length)
            write_runtime_phase()
            self.send_empty()
        elif urlparse(self.path).path == "/session/ses_lifecycle_fake/abort":
            self.send_json(True)
        else:
            self.send_json({"error": "not found"}, 404)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
print(
    f"opencode server listening on http://127.0.0.1:{server.server_address[1]}",
    flush=True,
)
server.serve_forever()
PY
chmod 755 "$BIN_DIR/opencode"

opencode_out="$TEST_ROOT/opencode-launch.out"
"$BIN_DIR/longhouse" opencode --no-attach --cwd "$HOME_DIR" --opencode-bin "$BIN_DIR/opencode" \
  >"$opencode_out" 2>&1 || {
    cat "$opencode_out" >&2
    fail "longhouse opencode failed against its protocol-faithful HTTP server"
  }
grep -q 'Managed OpenCode ready' "$opencode_out" || fail "OpenCode facade did not report readiness"
opencode_session_id="$(latest_launch_session_id)"
[[ -n "$opencode_session_id" ]] || fail "successful OpenCode launch did not record its session identity"
opencode_launch_state="$(launch_attempt_state "$opencode_session_id")"
[[ "$opencode_launch_state" == "pending" || "$opencode_launch_state" == "adopted" ]] \
  || fail "successful OpenCode launch recorded unexpected state $opencode_launch_state"
"$BIN_DIR/longhouse" opencode stop --session-id "$opencode_session_id" \
  || fail "OpenCode launch could not be torn down through the real facade"
echo "ok: longhouse opencode launched and stopped against an HTTP server"

set +e
LONGHOUSE_FAKE_OPENCODE_START_FAIL=1 \
  "$BIN_DIR/longhouse" opencode --no-attach --cwd "$HOME_DIR" --opencode-bin "$BIN_DIR/opencode" \
  >"$TEST_ROOT/opencode-start-failed.out" 2>&1
opencode_failed_status=$?
set -e
[[ "$opencode_failed_status" != "0" ]] || fail "scripted OpenCode startup failure returned success"
opencode_failed_session_id="$(latest_launch_session_id)"
[[ "$(launch_attempt_state "$opencode_failed_session_id")" == "failed" ]] \
  || fail "failed OpenCode startup did not abort its launch transaction"
echo "ok: OpenCode server startup failure aborts the registered launch"

# ---------------------------------------------------------------------------
# 3e. Start the real Machine Agent control channel and drive provider control
#     through the Runtime Host. These requests cross browser/API policy,
#     catalog authority, the machine WebSocket, native provider control, and
#     back again before the HTTP response succeeds.
# ---------------------------------------------------------------------------
"$BIN_DIR/longhouse" codex --no-attach --cwd "$HOME_DIR" --codex-bin "$BIN_DIR/codex" \
  >"$TEST_ROOT/codex-control-launch.out" 2>&1 \
  || fail "Codex control-cycle launch failed"
codex_control_session_id="$(latest_launch_session_id)"

"$BIN_DIR/longhouse" opencode --no-attach --cwd "$HOME_DIR" --opencode-bin "$BIN_DIR/opencode" \
  >"$TEST_ROOT/opencode-control-launch.out" 2>&1 \
  || fail "OpenCode control-cycle launch failed"
opencode_control_session_id="$(latest_launch_session_id)"

LONGHOUSE_FAKE_CLAUDE_CONTROL=1 \
  "$BIN_DIR/longhouse" claude --cwd "$HOME_DIR" --claude-bin "$BIN_DIR/claude" \
  >"$TEST_ROOT/claude-control-launch.out" 2>&1 &
CLAUDE_CONTROL_PID=$!
claude_state=""
for _ in $(seq 1 100); do
  claude_state="$(find "$HOME_DIR/.claude/channels/longhouse/sessions" -name '*.json' -type f 2>/dev/null | head -1 || true)"
  if [[ -n "$claude_state" ]] && [[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ready", False))' "$claude_state")" == "True" ]]; then
    break
  fi
  kill -0 "$CLAUDE_CONTROL_PID" 2>/dev/null || fail "Claude control-cycle provider exited before its channel became ready"
  sleep 0.2
done
[[ -n "$claude_state" ]] || fail "Claude control-cycle channel state never appeared"
claude_control_session_id="$(basename "$claude_state" .json)"

LONGHOUSE_FAKE_CURSOR_CONTROL=1 \
  run_launch_bounded "$TEST_ROOT/cursor-control-launch.out" 120 \
  "$BIN_DIR/longhouse" cursor --verbose --cwd "$HOME_DIR" --cursor-bin "$BIN_DIR/cursor-agent" &
CURSOR_CONTROL_PID=$!
cursor_phase=""
for _ in $(seq 1 100); do
  cursor_phase="$(find "$HOME_DIR/.longhouse/managed-local/cursor-helm" -name '*.phase.json' -type f 2>/dev/null | head -1 || true)"
  if [[ -n "$cursor_phase" ]] && [[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("phase", ""))' "$cursor_phase")" == "idle" ]]; then
    break
  fi
  kill -0 "$CURSOR_CONTROL_PID" 2>/dev/null || fail "Cursor control-cycle provider exited before its lifecycle hook became ready"
  sleep 0.2
done
[[ -n "$cursor_phase" ]] || fail "Cursor control-cycle phase state never appeared"
cursor_control_session_id="$(basename "$cursor_phase" .phase.json)"

# Launch the Machine Agent after both provider bridges exist. Its mandatory
# startup scan then publishes their control leases without depending on the
# slower periodic reconciliation path.
RUST_LOG=info "$BIN_DIR/longhouse-engine" connect \
  --url "$BASE_URL" \
  --token "$DEVICE_TOKEN" \
  --db "$TEST_ROOT/agent.db" \
  --machine-name "$DEVICE_ID" \
  --fallback-scan-secs 3600 \
  --spool-replay-secs 1 \
  >"$ENGINE_LOG" 2>&1 &
ENGINE_PID=$!
for _ in $(seq 1 100); do
  if grep -q 'WebSocket /api/agents/control/ws.*accepted' "$SERVER_LOG" 2>/dev/null; then
    break
  fi
  kill -0 "$ENGINE_PID" 2>/dev/null || fail "Machine Agent exited before its control channel connected"
  sleep 0.2
done
grep -q 'WebSocket /api/agents/control/ws.*accepted' "$SERVER_LOG" \
  || fail "Machine Agent control channel never connected"
wait_for_value "Codex durable launch outcome" adopted 20 launch_attempt_state "$codex_session_id"
wait_for_value "OpenCode durable launch outcome" adopted 20 launch_attempt_state "$opencode_session_id"
echo "ok: Machine Agent reconciled launch outcomes persisted before it started"

codex_send="$(send_live "$codex_control_session_id" 'CODEX_LIFECYCLE_CONTROL')" \
  || fail "Runtime Host failed to send through the Codex bridge"
[[ "$(printf '%s' "$codex_send" | json_field accepted)" == "True" ]] \
  || fail "Runtime Host did not accept the Codex send: $codex_send"
codex_interrupt="$(post_live_action "$codex_control_session_id" interrupt)" \
  || fail "Runtime Host failed to interrupt the Codex bridge"
[[ "$(printf '%s' "$codex_interrupt" | json_field interrupt_dispatched)" == "True" ]] \
  || fail "Runtime Host did not dispatch the Codex interrupt: $codex_interrupt"
"$BIN_DIR/longhouse" codex stop --session-id "$codex_control_session_id" \
  || fail "Codex control-cycle cleanup failed"
wait_for_value "Codex terminal state" session_ended 20 \
  runtime_terminal_state "$codex_control_session_id"
echo "ok: Codex send, interrupt, and terminal state crossed the real Runtime Host"

opencode_send="$(send_live "$opencode_control_session_id" 'OPENCODE_LIFECYCLE_CONTROL')" \
  || fail "Runtime Host failed to send through the OpenCode bridge"
[[ "$(printf '%s' "$opencode_send" | json_field accepted)" == "True" ]] \
  || fail "Runtime Host did not accept the OpenCode send: $opencode_send"
opencode_interrupt="$(post_live_action "$opencode_control_session_id" interrupt)" \
  || fail "Runtime Host failed to interrupt the OpenCode bridge"
[[ "$(printf '%s' "$opencode_interrupt" | json_field interrupt_dispatched)" == "True" ]] \
  || fail "Runtime Host did not dispatch the OpenCode interrupt: $opencode_interrupt"
opencode_terminate="$(post_live_action "$opencode_control_session_id" terminate)" \
  || fail "Runtime Host failed to terminate the OpenCode bridge"
[[ "$(printf '%s' "$opencode_terminate" | json_field terminate_dispatched)" == "True" ]] \
  || fail "Runtime Host did not dispatch OpenCode termination: $opencode_terminate"
wait_for_value "OpenCode terminal state" session_ended 20 \
  runtime_terminal_state "$opencode_control_session_id"
echo "ok: OpenCode send, interrupt, terminate, and terminal state crossed the real Runtime Host"

claude_send="$(send_live "$claude_control_session_id" 'CLAUDE_LIFECYCLE_CONTROL')" \
  || fail "Runtime Host failed to send through the Claude channel"
[[ "$(printf '%s' "$claude_send" | json_field accepted)" == "True" ]] \
  || fail "Runtime Host did not accept the Claude send: $claude_send"
claude_interrupt="$(post_live_action "$claude_control_session_id" interrupt)" \
  || fail "Runtime Host failed to interrupt Claude"
[[ "$(printf '%s' "$claude_interrupt" | json_field interrupt_dispatched)" == "True" ]] \
  || fail "Runtime Host did not dispatch the Claude interrupt: $claude_interrupt"
claude_provider_pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["claude_pid"])' "$claude_state")"
kill -TERM "$claude_provider_pid" || fail "could not stop the Claude control-cycle provider"
wait "$CLAUDE_CONTROL_PID" || fail "Claude control-cycle facade did not exit cleanly"
CLAUDE_CONTROL_PID=""
wait_for_value "Claude terminal state" session_ended 20 \
  runtime_terminal_state "$claude_control_session_id"
echo "ok: Claude send, interrupt, and terminal state crossed the real Runtime Host"

cursor_send="$(send_live "$cursor_control_session_id" 'CURSOR_LIFECYCLE_CONTROL')" \
  || fail "Runtime Host failed to send through Cursor Helm"
[[ "$(printf '%s' "$cursor_send" | json_field accepted)" == "True" ]] \
  || fail "Runtime Host did not accept the Cursor send: $cursor_send"
cursor_interrupt="$(post_live_action "$cursor_control_session_id" interrupt)" \
  || fail "Runtime Host failed to interrupt Cursor Helm"
[[ "$(printf '%s' "$cursor_interrupt" | json_field interrupt_dispatched)" == "True" ]] \
  || fail "Runtime Host did not dispatch the Cursor interrupt: $cursor_interrupt"
cursor_terminate="$(post_live_action "$cursor_control_session_id" terminate)" \
  || fail "Runtime Host failed to terminate Cursor Helm"
[[ "$(printf '%s' "$cursor_terminate" | json_field terminate_dispatched)" == "True" ]] \
  || fail "Runtime Host did not dispatch Cursor termination: $cursor_terminate"
set +e
wait "$CURSOR_CONTROL_PID"
cursor_control_status=$?
set -e
CURSOR_CONTROL_PID=""
[[ "$cursor_control_status" == "137" ]] \
  || fail "Cursor terminate returned unexpected facade status $cursor_control_status"
wait_for_value "Cursor terminal state" session_ended 20 \
  runtime_terminal_state "$cursor_control_session_id"
echo "ok: Cursor send, interrupt, terminate, and terminal state crossed the real Runtime Host"

# ---------------------------------------------------------------------------
# 4. A non-zero provider exit propagates rather than being swallowed.
# ---------------------------------------------------------------------------
set +e
LONGHOUSE_FAKE_CURSOR_EXIT=7 run_launch_bounded "$TEST_ROOT/cursor-exit.out" 90 \
  "$BIN_DIR/longhouse" cursor --cwd "$HOME_DIR" --cursor-bin "$BIN_DIR/cursor-agent"
cursor_exit=$?
set -e
[[ "$cursor_exit" == "7" ]] || fail "provider exit code was $cursor_exit, expected 7"
echo "ok: provider exit code propagates"

# ---------------------------------------------------------------------------
# 5. The Runtime Host can disappear before startup. Each managed provider must
#    still reach its local provider path, retain a durable registration retry,
#    and return a bounded degraded explanation. The provider executables here
#    are protocol-faithful launch doubles; the installed-provider version and
#    path checks belong to the release qualification lane below this smoke.
# ---------------------------------------------------------------------------
stop_process_tree "$SERVER_PID"
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
for _ in $(seq 1 50); do
  if ! curl -fsS --max-time 0.2 -o /dev/null "$BASE_URL/api/health" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
curl -fsS --max-time 0.2 -o /dev/null "$BASE_URL/api/health" 2>/dev/null \
  && fail "Runtime Host remained reachable after the outage transition"

degraded_cursor_out="$TEST_ROOT/cursor-degraded.out"
set +e
run_launch_bounded "$degraded_cursor_out" 30 \
  "$BIN_DIR/longhouse" cursor --url "$BASE_URL" --token "$DEVICE_TOKEN" \
  --cwd "$HOME_DIR" --cursor-bin "$BIN_DIR/cursor-agent"
degraded_cursor_status=$?
set -e
[[ "$degraded_cursor_status" == "0" ]] \
  || fail "Cursor did not return from a Runtime Host outage (status $degraded_cursor_status)"
grep -q 'CURSOR_LIFECYCLE_PTY_OK' "$degraded_cursor_out" \
  || fail "Cursor provider path did not start during a Runtime Host outage"
grep -q 'degraded Helm mode' "$degraded_cursor_out" \
  || { cat "$degraded_cursor_out" >&2; fail "Cursor outage launch did not explain degraded Helm mode"; }
echo "ok: Cursor starts locally with durable registration recovery while Runtime Host is unavailable"

degraded_claude_out="$TEST_ROOT/claude-degraded.out"
set +e
run_launch_bounded "$degraded_claude_out" 30 \
  "$BIN_DIR/longhouse" claude --url "$BASE_URL" --token "$DEVICE_TOKEN" \
  --cwd "$HOME_DIR" --claude-bin "$BIN_DIR/claude"
degraded_claude_status=$?
set -e
[[ "$degraded_claude_status" == "0" ]] \
  || fail "Claude did not return from a Runtime Host outage (status $degraded_claude_status)"
grep -q 'CLAUDE_LIFECYCLE_PTY_OK' "$degraded_claude_out" \
  || fail "Claude provider path did not start during a Runtime Host outage"
grep -q 'degraded Helm mode' "$degraded_claude_out" \
  || { cat "$degraded_claude_out" >&2; fail "Claude outage launch did not explain degraded Helm mode"; }
echo "ok: Claude starts locally with durable registration recovery while Runtime Host is unavailable"

degraded_codex_out="$TEST_ROOT/codex-degraded.out"
set +e
run_launch_bounded "$degraded_codex_out" 30 \
  "$BIN_DIR/longhouse" codex --url "$BASE_URL" --token "$DEVICE_TOKEN" \
  --no-attach --cwd "$HOME_DIR" --codex-bin "$BIN_DIR/codex"
degraded_codex_status=$?
set -e
[[ "$degraded_codex_status" == "0" ]] \
  || { cat "$degraded_codex_out" >&2; fail "Codex did not return from a Runtime Host outage (status $degraded_codex_status)"; }
grep -q 'Managed Codex started in degraded Helm mode' "$degraded_codex_out" \
  || fail "Codex provider path did not report degraded startup during a Runtime Host outage"
if grep -q 'Managed Codex ready' "$degraded_codex_out"; then
  fail "Codex outage launch claimed hosted readiness before registration recovered"
fi
grep -q 'degraded Helm mode' "$degraded_codex_out" \
  || { cat "$degraded_codex_out" >&2; fail "Codex outage launch did not explain degraded Helm mode"; }
echo "ok: Codex starts locally with durable registration recovery while Runtime Host is unavailable"

degraded_opencode_out="$TEST_ROOT/opencode-degraded.out"
set +e
run_launch_bounded "$degraded_opencode_out" 30 \
  "$BIN_DIR/longhouse" opencode --url "$BASE_URL" --token "$DEVICE_TOKEN" \
  --no-attach --cwd "$HOME_DIR" --opencode-bin "$BIN_DIR/opencode"
degraded_opencode_status=$?
set -e
[[ "$degraded_opencode_status" == "0" ]] \
  || { cat "$degraded_opencode_out" >&2; fail "OpenCode did not return from a Runtime Host outage (status $degraded_opencode_status)"; }
grep -q 'Managed OpenCode started in degraded Helm mode' "$degraded_opencode_out" \
  || fail "OpenCode provider path did not report degraded startup during a Runtime Host outage"
if grep -q 'Managed OpenCode ready' "$degraded_opencode_out"; then
  fail "OpenCode outage launch claimed hosted readiness before registration recovered"
fi
grep -q 'degraded Helm mode' "$degraded_opencode_out" \
  || { cat "$degraded_opencode_out" >&2; fail "OpenCode outage launch did not explain degraded Helm mode"; }
echo "ok: OpenCode starts locally with durable registration recovery while Runtime Host is unavailable"

registration_retry_dir="$HOME_DIR/.longhouse/agent/managed-local/registration-retries"
wait_for_retry_files_at_least "durable registration retry intents" 4 10 "$registration_retry_dir"
retry_session_ids_file="$TEST_ROOT/registration-retry-session-ids.txt"
python3 - "$registration_retry_dir" >"$retry_session_ids_file" <<'PY'
import json
import pathlib
import sys

directory = pathlib.Path(sys.argv[1])
for path in sorted(directory.glob("*.json")):
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        continue
    session_id = payload.get("expected_session_id")
    if session_id:
        print(session_id)
PY
retry_session_count="$(wc -l <"$retry_session_ids_file" | tr -d ' ')"
[[ "$retry_session_count" == "4" ]] \
  || fail "expected four recoverable launch identities, found $retry_session_count"
echo "ok: outage launches persisted durable registration retry intents"

# Bring the same Runtime Host identity back. The Machine Agent is still alive,
# so this proves the daemon's owner-local queues converge without relaunching a
# provider or asking the user to click Repair.
export LONGHOUSE_LIFECYCLE_SMOKE_PORT="$PORT"
start_runtime_host || fail "Runtime Host did not recover on its original port"
echo "runtime host recovered on $BASE_URL"
wait_for_value "recovered Runtime Host" "200" 20 curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/health"
wait_for_value "registration retry convergence" "0" 60 retry_file_count "$registration_retry_dir"
recovered_adopted=0
recovered_failed=0
while IFS= read -r retry_session_id; do
  [[ -n "$retry_session_id" ]] || continue
  retry_outcome="$(wait_for_launch_terminal_state "$retry_session_id" 30)"
  case "$retry_outcome" in
    adopted)
      recovered_adopted=$((recovered_adopted + 1))
      ;;
    failed)
      recovered_failed=$((recovered_failed + 1))
      ;;
    abandoned)
      fail "Runtime Host abandoned recovered launch $retry_session_id"
      ;;
  esac
  echo "ok: Runtime Host recorded $retry_outcome for recovered launch $retry_session_id"
done <"$retry_session_ids_file"
((recovered_adopted > 0)) \
  || fail "Runtime Host recorded no adopted outcome for the recovered launch set"
echo "ok: recovered launch set included $recovered_adopted adopted and $recovered_failed failed provider outcomes"
echo "ok: durable managed launch recovery converged after Runtime Host restart"

echo "managed launch lifecycle smoke passed"
