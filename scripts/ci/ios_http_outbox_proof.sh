#!/usr/bin/env bash
# Native-isolation-only runner for the real HTTP/PhotosPicker outbox proof.
# Called by ios_ui_shot.sh; it owns the fixture process, simulator, Photos seed,
# result bundle, and all teardown. No host credentials or provider archives enter.
# Invocation (through native isolation only):
#   make ios-ui-shot TEST=HTTPOutboxUITests/testRealHTTPOutboxPhotosPickerSurvivesTerminateAndReopen
set -euo pipefail

TEST="${1:?test id required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if ! python3 "$ROOT/scripts/qa/test_boundary.py"; then
  echo "HTTP outbox proof requires make ios-ui-shot in native isolation." >&2
  exit 2
fi
PROJECT="$ROOT/ios/XcodeHarness/LonghouseIOS.xcodeproj"
SCHEME="LonghouseSmoke"
DERIVED_DATA_PATH="${IOS_DERIVED_DATA_PATH:-${HOME}/Library/Developer/Xcode/DerivedData/LonghouseIOS-HTTPProof}"
OUT="$ROOT/artifacts/ios-http-outbox-proof/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
chmod 700 "$OUT"

FIXTURE_PID=""
SIM_UDID=""
RESULT_STATUS=1

cleanup() {
  local status=$?
  set +e
  if [[ -n "$FIXTURE_PID" ]]; then
    kill -TERM "$FIXTURE_PID" 2>/dev/null || true
    wait "$FIXTURE_PID" 2>/dev/null || true
  fi
  if [[ -n "$SIM_UDID" ]]; then
    xcrun simctl shutdown "$SIM_UDID" >/dev/null 2>&1 || true
    xcrun simctl delete "$SIM_UDID" >/dev/null 2>&1 || true
  fi
  echo "HTTP outbox proof evidence: $OUT" >&2
  exit "$status"
}
trap cleanup EXIT INT TERM

SESSION_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
AUTH_TOKEN="ios-http-proof-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
)"
READY="$OUT/fixture-ready.json"
STATE="$OUT/fixture-state.json"
python3 "$ROOT/scripts/qa/ios_http_outbox_fixture.py" \
  --session-id "$SESSION_ID" \
  --auth-token "$AUTH_TOKEN" \
  --state "$STATE" \
  --ready "$READY" \
  --port 0 >"$OUT/fixture.log" 2>&1 &
FIXTURE_PID=$!
for _ in $(seq 1 200); do
  if [[ -s "$READY" ]]; then break; fi
  if ! kill -0 "$FIXTURE_PID" 2>/dev/null; then
    cat "$OUT/fixture.log" >&2
    exit 1
  fi
  sleep 0.1
done
[[ -s "$READY" ]] || { cat "$OUT/fixture.log" >&2; exit 1; }
SERVER_URL="$(python3 - "$READY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["server_url"])
PY
)"
STATE_URL="$(python3 - "$READY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["state_url"])
PY
)"
ENABLE_RECEIPT_URL="$(python3 - "$READY" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["enable_receipt_url"])
PY
)"

# Always use a newly-created simulator: Photos contains only this proof's
# disposable seed and is deleted by cleanup, never a user's existing device.
read -r DEVICE_TYPE RUNTIME < <(python3 - <<'PY'
import json, subprocess
runtimes = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "runtimes", "available", "-j"], text=True))["runtimes"]
runtimes = [r for r in runtimes if "iOS" in r.get("identifier", "") and r.get("isAvailable")]
runtime = sorted(runtimes, key=lambda r: r.get("version", ""))[-1]
phones = [d for d in runtime.get("supportedDeviceTypes", []) if d.get("productFamily") == "iPhone"]
if not phones:
    raise SystemExit("no iPhone type supported by the selected iOS runtime")
print(phones[0]["identifier"], runtime["identifier"])
PY
)
SIM_UDID="$(xcrun simctl create "Longhouse HTTP Outbox Proof ${SESSION_ID:0:8}" "$DEVICE_TYPE" "$RUNTIME")"
xcrun simctl boot "$SIM_UDID"
xcrun simctl bootstatus "$SIM_UDID" -b

SEED="$OUT/photos-proof.png"
python3 - "$SEED" <<'PY'
import base64, sys
# A disposable 1x1 PNG; simctl addmedia imports it through the real Photos
# library, and PHPicker subsequently supplies the bytes to the app.
data = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
open(sys.argv[1], "wb").write(data)
PY
xcrun simctl addmedia "$SIM_UDID" "$SEED"

export TEST_RUNNER_LONGHOUSE_HEADLESS_SERVER_URL="$SERVER_URL"
export TEST_RUNNER_LONGHOUSE_HEADLESS_AUTH_TOKEN="$AUTH_TOKEN"
export TEST_RUNNER_LONGHOUSE_HEADLESS_OPEN_SESSION="$SESSION_ID"
export TEST_RUNNER_LONGHOUSE_HTTP_PROOF_STATE_URL="$STATE_URL"
export TEST_RUNNER_LONGHOUSE_HTTP_PROOF_ENABLE_RECEIPT_URL="$ENABLE_RECEIPT_URL"
export TEST_RUNNER_LONGHOUSE_HTTP_PROOF_SESSION_ID="$SESSION_ID"

make -C "$ROOT" ios-project
DESTINATION="platform=iOS Simulator,id=$SIM_UDID"
set +e
xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
  -derivedDataPath "$DERIVED_DATA_PATH" build-for-testing 2>&1 | tee "$OUT/build.log"
BUILD_STATUS=${PIPESTATUS[0]}
if [[ "$BUILD_STATUS" -ne 0 ]]; then
  echo "iOS HTTP outbox proof build failed (exit $BUILD_STATUS)" >&2
  exit "$BUILD_STATUS"
fi
xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
  -derivedDataPath "$DERIVED_DATA_PATH" -resultBundlePath "$OUT/result.xcresult" \
  -collect-test-diagnostics never \
  -only-testing:"LonghouseIOSUITests/$TEST" test-without-building 2>&1 | tee "$OUT/test.log"
RESULT_STATUS=${PIPESTATUS[0]}
set -e

xcrun xcresulttool export attachments --path "$OUT/result.xcresult" --output-path "$OUT/attachments" >/dev/null 2>&1 || true
printf 'result_bundle=%s\nfixture_state=%s\n' "$OUT/result.xcresult" "$STATE"
exit "$RESULT_STATUS"
