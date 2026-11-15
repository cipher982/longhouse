#!/usr/bin/env bash
# Live receipt for the Antigravity Console adapter (engine/src/antigravity_acp.rs):
# a real `agy acp` behind the credential-free scripted Gemini server, in a scratch
# HOME, driven through the adapter's own entry points (new turn, resume, steer with an
# image, interrupt). Run it on a bench host, never on the dev Mac:
#
#   scripts/ops/bench.sh run scripts/qa/agy-acp-live-receipt.sh
#
# Run by hand: it downloads a provider release and drives a real agy, which never runs in CI or on
# the dev Mac; the factory's Console lifecycle producer (antigravity-parity.md step 6) is its
# automated successor.
#
# The receipt lands in $BENCH_OUT (bench.sh copies it back to /tmp/agents/bench/<stamp>/).
# Everything it starts lives under one scratch directory and is removed on exit.
set -euo pipefail

AGY_VERSION="${AGY_VERSION:-1.5.9}"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) AGY_ASSET=agy_cli_mac_arm64.tar.gz; AGY_SHA256=51fb8eb7e18c53d4665f0043b761f04f4d0d4b2eb046bac6c2dcbe32473b5b2e ;;
  *) echo "agy-live-receipt: no pinned agy asset for $(uname -s)-$(uname -m)" >&2; exit 2 ;;
esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${BENCH_OUT:-$ROOT/.agy-live-out}"
mkdir -p "$OUT"
SCRATCH="$(mktemp -d /tmp/agy-live.XXXXXX)"
SERVER_PID=""

cleanup() {
  local rc=$?
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  # Anything still running out of the scratch tree (agy, its tools) is ours.
  pkill -f "$SCRATCH" 2>/dev/null || true
  sleep 1
  pkill -9 -f "$SCRATCH" 2>/dev/null || true
  {
    echo "leftover_processes: $(pgrep -f "$SCRATCH" | wc -l | tr -d ' ')"
  } >"$OUT/cleanup.txt"
  rm -rf "$SCRATCH"
  [ -e "$SCRATCH" ] && echo "scratch_removed: no" >>"$OUT/cleanup.txt" || echo "scratch_removed: yes" >>"$OUT/cleanup.txt"
  exit "$rc"
}
trap cleanup EXIT INT TERM

# 1. The pinned agy, verified against the release digest.
mkdir -p "$SCRATCH/dl" "$SCRATCH/bin"
curl -fsSL -o "$SCRATCH/dl/$AGY_ASSET" "https://github.com/google-antigravity/antigravity-cli/releases/download/$AGY_VERSION/$AGY_ASSET"
echo "$AGY_SHA256  $SCRATCH/dl/$AGY_ASSET" | shasum -a 256 -c -
tar -xzf "$SCRATCH/dl/$AGY_ASSET" -C "$SCRATCH/bin"
AGY_BIN="$(find "$SCRATCH/bin" -type f \( -name agy -o -name antigravity \) | head -1)"
chmod +x "$AGY_BIN"

# 2. The scripted model. Earlier turns win, so the steer, resume and tool-result
# markers come before the markers they would otherwise be shadowed by.
cat >"$SCRATCH/script.json" <<'JSON'
{"turns": [
  {"id": "steer-ack", "match": "LIVE_STEER_NOW", "match_scope": "any_user", "reply": "STEER_ACK"},
  {"id": "tool-done", "match": "LIVE_TOOL_DONE", "reply": "TOOL_DONE_REPLY"},
  {"id": "cancel-start", "match": "LIVE_CANCEL_START", "match_scope": "any_user", "reply": "CANCEL_STARTED",
   "tool_call": {"type": "shell", "name": "shell", "command": "sleep 60 && echo LIVE_TOOL_DONE"}},
  {"id": "steer-start", "match": "LIVE_STEER_START", "match_scope": "any_user", "reply": "STEER_STARTED",
   "tool_call": {"type": "shell", "name": "shell", "command": "sleep 20 && echo LIVE_TOOL_DONE"}},
  {"id": "resume", "match": "LIVE_RESUME", "match_scope": "any_user", "reply": "RESUMED_OK"},
  {"id": "ping", "match": "LIVE_PING", "match_scope": "any_user", "reply": "LIVE_PONG"}
]}
JSON
mkdir -p "$SCRATCH/dump" "$SCRATCH/home" "$SCRATCH/lh" "$SCRATCH/cwd"
python3 "$ROOT/server/zerg/qa/scripted_model_server.py" --script "$SCRATCH/script.json" \
  --port-file "$SCRATCH/port" --dump-requests "$SCRATCH/dump" --receipt-file "$SCRATCH/server-receipt.json" \
  >"$SCRATCH/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do [ -s "$SCRATCH/port" ] && break; sleep 0.2; done
[ -s "$SCRATCH/port" ] || { echo "scripted model server did not start" >&2; cat "$SCRATCH/server.log" >&2; exit 1; }
ENDPOINT="http://127.0.0.1:$(cat "$SCRATCH/port")"

# 3. Build the test binary under the real HOME, then run it under the scratch one.
cd "$ROOT"
python3 scripts/build/generate_build_identity.py >/dev/null
TEST_BIN="$(cargo test --manifest-path engine/Cargo.toml --features qa-fault-injection --bin longhouse-engine --no-run --message-format json 2>"$SCRATCH/build.log" \
  | python3 -c 'import json,sys
for line in sys.stdin:
    try: m=json.loads(line)
    except ValueError: continue
    if m.get("reason")=="compiler-artifact" and m.get("executable") and m.get("profile",{}).get("test"): print(m["executable"])' | tail -1)"
[ -n "$TEST_BIN" ] || { tail -30 "$SCRATCH/build.log" >&2; echo "no test binary" >&2; exit 1; }

# The profile writer sets HOME, the endpoint and the proxy for agy; print them as exports.
ENV_FILE="$SCRATCH/profile.env"
LONGHOUSE_SCRIPTED_MODEL_BASE_URL="$ENDPOINT" PYTHONPATH="$ROOT/server" python3 - "$SCRATCH/home" >"$ENV_FILE" <<'PY'
import os, shlex, sys
from pathlib import Path
from zerg.qa.scripted_model_profiles import configure_scripted_model_profile
env = dict(os.environ)
configure_scripted_model_profile("antigravity", Path(sys.argv[1]), env)
for key in ("HOME", "GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
    print(f"export {key}={shlex.quote(env[key])}")
PY

set +e
(
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  export LONGHOUSE_HOME="$SCRATCH/lh" LONGHOUSE_ANTIGRAVITY_BIN="$AGY_BIN" LIVE_CWD="$SCRATCH/cwd" LIVE_RECEIPT_FILE="$OUT/receipt.json"
  "$TEST_BIN" --ignored --exact antigravity_acp::tests::live_agy_console_receipt --nocapture --test-threads=1
) >"$OUT/test.log" 2>&1
TEST_RC=$?
set -e
cp "$SCRATCH/server-receipt.json" "$OUT/server-receipt.json" 2>/dev/null || true
cp "$SCRATCH/server.log" "$OUT/server.log" 2>/dev/null || true
mkdir -p "$OUT/requests" && cp "$SCRATCH"/dump/* "$OUT/requests/" 2>/dev/null || true
cp "$SCRATCH"/home/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript_full.jsonl "$OUT/" 2>/dev/null || true
echo "agy $AGY_VERSION sha256 $AGY_SHA256; test exit $TEST_RC" >"$OUT/summary.txt"
exit "$TEST_RC"
