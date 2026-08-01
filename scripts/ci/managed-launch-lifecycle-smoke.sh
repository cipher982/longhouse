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
# Codex's per-session Unix socket must fit the platform SUN_LEN limit. Keep the
# smoke root short so the test exercises lifecycle behavior rather than the
# host's unusually long default macOS TMPDIR prefix.
TEST_ROOT="$(mktemp -d /tmp/lh.XXXXXX)"
HOME_DIR="$TEST_ROOT/home"
BIN_DIR="$TEST_ROOT/bin"
SERVER_LOG="$TEST_ROOT/server.log"
SERVER_PID=""
PORT="${LONGHOUSE_LIFECYCLE_SMOKE_PORT:-0}"

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
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
      LOG_LEVEL=WARNING \
      DATABASE_URL="sqlite:///$TEST_ROOT/longhouse.db" \
      JWT_SECRET="lifecycle-smoke-jwt-secret" \
      FERNET_SECRET="$FERNET" \
      INTERNAL_API_SECRET="lifecycle-smoke-internal-secret" \
      uv run python -m zerg.cli.main serve --host 127.0.0.1 --port "$PORT" >"$SERVER_LOG" 2>&1
    ) &
    SERVER_PID=$!

    local _
    for _ in $(seq 1 120); do
      if curl -fsS -o /dev/null "$BASE_URL/api/health" 2>/dev/null; then return 0; fi
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
      sleep 0.5
    done

    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
    if [[ "${LONGHOUSE_LIFECYCLE_SMOKE_PORT:-0}" != "0" ]]; then
      break
    fi
    echo "runtime host did not come up on port $PORT; retrying on a new port" >&2
  done
  return 1
}

FERNET="$(cd "$ROOT_DIR/server" && uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

start_runtime_host || fail "Runtime Host never became healthy"
echo "runtime host up on $BASE_URL"

DEVICE_TOKEN="$(curl -fsS -X POST "$BASE_URL/api/devices/tokens" \
  -H 'content-type: application/json' \
  -d '{"name":"lifecycle-smoke","device_id":"lifecycle-smoke-host"}' \
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
  curl -fsS -X POST "$BASE_URL/api/sessions/managed-local/this-device" \
    -H "X-Agents-Token: $DEVICE_TOKEN" \
    -H 'content-type: application/json' \
    -d "$(launch_payload "$provider" "$session_id")"
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
cat > "$BIN_DIR/cursor-agent" <<'EOF'
#!/usr/bin/env sh
if [ "$1" = "create-chat" ]; then
  if [ "${LONGHOUSE_FAKE_CURSOR_CREATE_FAIL:-0}" = "1" ]; then
    printf '%s\n' 'scripted create-chat failure' >&2
    exit 9
  fi
  printf '%s\n' '00000000-0000-0000-0000-000000000001'
  exit 0
fi
printf '%s\n' 'CURSOR_LIFECYCLE_PTY_OK'
sleep 1
exit "${LONGHOUSE_FAKE_CURSOR_EXIT:-0}"
EOF
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
cat > "$BIN_DIR/claude" <<'EOF'
#!/usr/bin/env sh
if [ "$1" = "auth" ]; then
  printf '%s\n' '{"loggedIn": true}'
  exit 0
fi
printf '%s\n' 'CLAUDE_LIFECYCLE_PTY_OK'
exit "${LONGHOUSE_FAKE_CLAUDE_EXIT:-0}"
EOF
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
        else:
            result = {}
        if request_id is not None:
            websocket.send(json.dumps({"id": request_id, "result": result}))


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
[[ "$(launch_attempt_state "$codex_session_id")" == "adopted" ]] \
  || fail "successful Codex launch was not adopted"
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
import signal
import sys
from urllib.parse import urlparse

if os.environ.get("LONGHOUSE_FAKE_OPENCODE_START_FAIL") == "1":
    print("scripted OpenCode server startup failure", flush=True)
    raise SystemExit(9)

if not sys.argv[1:] or sys.argv[1] != "serve":
    print("unexpected fake opencode args: " + json.dumps(sys.argv[1:]), file=sys.stderr)
    raise SystemExit(2)

username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")


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
[[ "$(launch_attempt_state "$opencode_session_id")" == "adopted" ]] \
  || fail "successful OpenCode launch was not adopted"
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
# 4. A non-zero provider exit propagates rather than being swallowed.
# ---------------------------------------------------------------------------
set +e
LONGHOUSE_FAKE_CURSOR_EXIT=7 run_launch_bounded "$TEST_ROOT/cursor-exit.out" 90 \
  "$BIN_DIR/longhouse" cursor --cwd "$HOME_DIR" --cursor-bin "$BIN_DIR/cursor-agent"
cursor_exit=$?
set -e
[[ "$cursor_exit" == "7" ]] || fail "provider exit code was $cursor_exit, expected 7"
echo "ok: provider exit code propagates"

echo "managed launch lifecycle smoke passed"
