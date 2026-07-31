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
TEST_ROOT="$(mktemp -d)"
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
  rm -rf "$TEST_ROOT"
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
if [[ "$PORT" == "0" ]]; then
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
fi
BASE_URL="http://127.0.0.1:$PORT"

FERNET="$(cd "$ROOT_DIR/server" && uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

(
  cd "$ROOT_DIR/server"
  # Deliberately NOT TESTING=1: that forces live_catalog_enabled() false, which
  # short-circuits the coordination-token endpoint at its 503 guard before the
  # provider check and leaves the live store unused. A counterpart that refuses
  # for the wrong reason is only marginally better than one that never refuses.
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

for _ in $(seq 1 120); do
  if curl -fsS -o /dev/null "$BASE_URL/api/health" 2>/dev/null; then break; fi
  sleep 0.5
done
curl -fsS -o /dev/null "$BASE_URL/api/health" || fail "Runtime Host never became healthy"
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

# `longhouse <provider>` exits cleanly, but the PTY master does not always reach
# EOF: something in the managed launch keeps the slave fd open after the child
# is reaped, so a plain `run-in-pty.py` invocation can block forever waiting to
# read. That is a real teardown observation (recorded for the launch-lifecycle
# work) and it must never become an unbounded CI hang, so every launch here is
# bounded and judged on observable outcomes -- what the provider printed, what
# reached the Runtime Host, and the exit status -- rather than on the wrapper
# seeing EOF.
run_launch_bounded() {
  local out_file="$1" timeout_secs="$2"
  shift 2
  python3 - "$out_file" "$timeout_secs" "$@" <<'PYEOF'
import os, pty, select, subprocess, sys, time

out_path, timeout_secs = sys.argv[1], float(sys.argv[2])
argv = sys.argv[3:]
master, slave = pty.openpty()
proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave)
chunks, deadline = [], time.time() + timeout_secs
while time.time() < deadline:
    ready, _, _ = select.select([master], [], [], 0.25)
    if ready:
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    if proc.poll() is not None:
        # Drain briefly, then stop: another fd holder can keep the master open.
        drain_until = time.time() + 0.5
        while time.time() < drain_until:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                break
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        break
os.close(master)
code = proc.poll()
if code is None:
    proc.kill()
    proc.wait()
    code = 124
with open(out_path, "wb") as handle:
    handle.write(b"".join(chunks))
sys.exit(code if code >= 0 else 128 - code)
PYEOF
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

LONGHOUSE_DEVICE_TOKEN="$DEVICE_TOKEN" "$BIN_DIR/longhouse" auth --url "$BASE_URL" >/dev/null
[[ -f "$HOME_DIR/.longhouse/machine/device-token" ]] || fail "device token was not stored"

launch_out="$TEST_ROOT/cursor-launch.out"
set +e
run_launch_bounded "$launch_out" 90 \
  "$BIN_DIR/longhouse" cursor --cwd "$HOME_DIR" --cursor-bin "$BIN_DIR/cursor-agent"
launch_status=$?
set -e
if [[ "$launch_status" != "0" ]]; then
  echo "--- launch output ---" >&2
  cat "$launch_out" >&2
  fail "longhouse cursor exited $launch_status against a real Runtime Host"
fi
grep -q 'CURSOR_LIFECYCLE_PTY_OK' "$launch_out" || fail "the scripted provider never ran under the PTY"
echo "ok: longhouse cursor launched against a real Runtime Host"

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
