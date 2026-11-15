#!/usr/bin/env bash
# Live receipt for Antigravity Helm control (engine/src/antigravity_channel_control.rs):
# a real agy behind the credential-free scripted Gemini server, in a scratch HOME with the
# real Longhouse hook installed, driven through the engine's own steer, interrupt and
# terminate entry points (steer with an image path, interrupt, terminate with the tool child).
# Run it on a bench host, never on the dev Mac:
#
#   scripts/ops/bench.sh run scripts/qa/agy-helm-live-receipt.sh
#
# Run by hand: it downloads a provider release and drives a real agy, which never runs in CI or on
# the dev Mac; the factory's Helm lifecycle producer (antigravity-parity.md step 6) is its
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
  {"id": "steer-ack", "match": "HELM_STEER_NOW", "match_scope": "any_user", "reply": "STEER_ACK"},
  {"id": "tool-done", "match": "HELM_TOOL_DONE", "reply": "TOOL_DONE_REPLY"},
  {"id": "term-start", "match": "HELM_TERM_START", "match_scope": "any_user", "reply": "TERM_STARTED",
   "tool_call": {"type": "shell", "name": "shell", "command": "sleep 61 && echo HELM_TOOL_DONE"}},
  {"id": "int-start", "match": "HELM_INT_START", "match_scope": "any_user", "reply": "INT_STARTED",
   "tool_call": {"type": "shell", "name": "shell", "command": "sleep 8 && echo HELM_TOOL_DONE"}},
  {"id": "steer-start", "match": "HELM_STEER_START", "match_scope": "any_user", "reply": "STEER_STARTED",
   "tool_call": {"type": "shell", "name": "shell", "command": "sleep 12 && echo HELM_TOOL_DONE"}}
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

# The real Longhouse hook, installed into the scratch HOME's agy the way onboard does.
(
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  PYTHONPATH="$ROOT/server" python3 - "$SCRATCH/lh" "$AGY_BIN" <<'PY'
import sys
from pathlib import Path
from zerg.services.antigravity_hook_inbox import _ensure_antigravity_runtime_plugin
_ensure_antigravity_runtime_plugin(config_dir=Path(sys.argv[1]), antigravity_bin=sys.argv[2])
PY
) >"$OUT/hook-install.log" 2>&1

set +e
(
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  export LONGHOUSE_HOME="$SCRATCH/lh" LONGHOUSE_ANTIGRAVITY_BIN="$AGY_BIN" LIVE_CWD="$SCRATCH/cwd" LIVE_RECEIPT_FILE="$OUT/receipt.json"
  "$TEST_BIN" --ignored --exact antigravity_channel_control::tests::live_agy_helm_receipt --nocapture --test-threads=1
) >"$OUT/test.log" 2>&1
TEST_RC=$?
set -e
cp "$SCRATCH/server-receipt.json" "$OUT/server-receipt.json" 2>/dev/null || true
cp "$SCRATCH/server.log" "$OUT/server.log" 2>/dev/null || true
mkdir -p "$OUT/requests" && cp "$SCRATCH"/dump/* "$OUT/requests/" 2>/dev/null || true
cp "$SCRATCH"/home/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript_full.jsonl "$OUT/" 2>/dev/null || true
# What the model saw: the steer text and the image path must be in a request body; an
# interrupt must stop further model calls after the tool.
python3 - "$SCRATCH/dump" "$OUT/receipt.json" >"$OUT/model-requests.txt" <<'PY' || true
import glob, json, sys
dump, receipt_path = sys.argv[1], sys.argv[2]
bodies = {}
for path in sorted(glob.glob(dump + "/*")):
    try: bodies[path.rsplit("/", 1)[-1]] = open(path, errors="replace").read()
    except OSError: pass
receipt = json.load(open(receipt_path)) if receipt_path else {}
png = (receipt.get("steer") or {}).get("image_path", "")
print("requests:", len(bodies))
print("steer_marker_in_a_request:", any("HELM_STEER_NOW" in b for b in bodies.values()))
print("image_path_in_the_steer_request:", bool(png) and any(png in b and "HELM_STEER_NOW" in b for b in bodies.values()))
print("tool_done_after_interrupt_requests:", sum("HELM_TOOL_DONE" in b and "HELM_INT_START" in b for b in bodies.values()))
PY
echo "agy $AGY_VERSION sha256 $AGY_SHA256; test exit $TEST_RC" >"$OUT/summary.txt"
exit "$TEST_RC"
