#!/usr/bin/env bash
# The live lane is deliberately separate from credential-free hosted ios-ui-shot.
# It runs only on wisp, where bench.sh serializes work and this script owns a disposable simulator.
# Credentials never enter workflow-dispatch inputs or GitHub logs.
#
#   LONGHOUSE_LIVE_CONTROL_SERVER_URL=https://tenant.example \
#   LONGHOUSE_LIVE_CONTROL_AUTH_TOKEN=zdt_... \
#   LONGHOUSE_LIVE_CONTROL_SESSION_ID=<real-omp-console-session> \
#   IOS_LIVE_CONTROL_PHOTO=/tmp/agents/ios-control/<run>/real-photo.jpg \
#   scripts/ci/ios_live_control_proof.sh \
#     LiveConsoleControlUITests/testLiveConsoleControlAndPhoto
set -euo pipefail
TEST="${1:?test id required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == "wisp" ]] || {
  echo "Live iOS control proof is refused outside wisp; use ios-ui-shot for credential-free hosted QA." >&2
  exit 2
}
if ! python3 "$ROOT/scripts/qa/test_boundary.py"; then
  case "${BENCH_OUT:-}" in
    /tmp/agents/bench-out/*) ;;
    *) echo "Live iOS control proof requires scripts/ops/bench.sh on wisp." >&2; exit 2 ;;
  esac
fi

required_env() {
  local key="$1"
  [[ -n "${!key:-}" ]] || { echo "Live iOS control proof requires $key" >&2; exit 2; }
}
required_env LONGHOUSE_LIVE_CONTROL_SERVER_URL
required_env LONGHOUSE_LIVE_CONTROL_AUTH_TOKEN
required_env LONGHOUSE_LIVE_CONTROL_SESSION_ID
required_env IOS_LIVE_CONTROL_PHOTO
[[ -f "$IOS_LIVE_CONTROL_PHOTO" ]] || { echo "photo does not exist: $IOS_LIVE_CONTROL_PHOTO" >&2; exit 2; }
[[ "$TEST" == "LiveConsoleControlUITests/testLiveConsoleControlAndPhoto" ]] || {
  echo "unsupported live iOS control test: $TEST" >&2
  exit 2
}

PROJECT="$ROOT/ios/XcodeHarness/LonghouseIOS.xcodeproj"
SCHEME="LonghouseSmoke"
DERIVED_DATA_PATH="${IOS_DERIVED_DATA_PATH:-${HOME}/Library/Developer/Xcode/DerivedData/LonghouseIOS-Sim}"
OUT_BASE="${IOS_LIVE_CONTROL_OUT_DIR:-${BENCH_OUT:-$ROOT/artifacts}/ios-live-control-proof}"
OUT="$OUT_BASE/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
chmod 700 "$OUT"

SIM_UDID=""
STATUS=1
declare -a OWNER_PIDS=()
declare -a OWNER_STARTS=()
declare -a OWNER_PGIDS=()
declare -a OWNER_PGID_PIDS=()
declare -a OWNER_PGID_STARTS=()
SHELL_PGID="$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ' || true)"
LAST_PID=""
LAST_PGID=""
LAST_START=""

process_start() {
  local value
  value="$(ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | xargs 2>/dev/null || true)"
  [[ -n "$value" ]] || return 1
  printf '%s' "$value"
}

# Return 0 only when the PID still has the exact birth identity recorded at
# launch; 1 means it is gone, and 2 means it was reused or identity was absent.
owned_identity_state() {
  local pid="$1"
  local expected="$2"
  local current
  current="$(process_start "$pid" 2>/dev/null || true)"
  [[ -n "$current" ]] || return 1
  [[ -n "$expected" && "$current" == "$expected" ]] && return 0
  return 2
}

start_owned() {
  local log="$1"
  shift
  python3 -c 'import os, subprocess, sys; log_path, *argv = sys.argv[1:]; os.setsid(); log = open(log_path, "wb"); result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT); log.close(); raise SystemExit(result.returncode)' "$log" "$@" &
  LAST_PID=$!
  OWNER_PIDS+=("$LAST_PID")
  LAST_START="$(process_start "$LAST_PID" 2>/dev/null || true)"
  OWNER_STARTS+=("$LAST_START")
  if [[ -z "$LAST_START" ]]; then
    echo "could not record birth identity for owned iOS proof process $LAST_PID" >&2
    return 1
  fi
  for _ in 1 2 3 4 5; do
    LAST_PGID="$(ps -o pgid= -p "$LAST_PID" 2>/dev/null | tr -d ' ' || true)"
    [[ -n "$LAST_PGID" && "$LAST_PGID" != "$SHELL_PGID" ]] && break
    sleep 0.05
  done
  if [[ -n "$LAST_PGID" && "$LAST_PGID" != "$SHELL_PGID" ]]; then
    OWNER_PGIDS+=("$LAST_PGID")
    OWNER_PGID_PIDS+=("$LAST_PID")
    OWNER_PGID_STARTS+=("$LAST_START")
  fi
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  set +e
  for index in "${!OWNER_PGIDS[@]}"; do
    pgid="${OWNER_PGIDS[$index]}"
    owner_pid="${OWNER_PGID_PIDS[$index]}"
    expected="${OWNER_PGID_STARTS[$index]}"
    if owned_identity_state "$owner_pid" "$expected"; then
      kill -TERM "-$pgid" >/dev/null 2>&1 || true
    else
      identity_status=$?
      if (( identity_status == 2 )); then
        echo "refusing to signal reused iOS proof process-group leader: $owner_pid" >&2
        cleanup_failed=1
      fi
    fi
  done
  for index in "${!OWNER_PIDS[@]}"; do
    pid="${OWNER_PIDS[$index]}"
    expected="${OWNER_STARTS[$index]}"
    if owned_identity_state "$pid" "$expected"; then
      kill -TERM "$pid" >/dev/null 2>&1 || true
    else
      identity_status=$?
      if (( identity_status == 2 )); then
        echo "refusing to signal reused iOS proof process: $pid" >&2
        cleanup_failed=1
      fi
    fi
  done

  # xcodebuild can take a short time to release its child processes. Bound the
  # graceful wait, then kill only the process groups this invocation created.
  local deadline=$((SECONDS + 20))
  while (( SECONDS < deadline )); do
    local alive=0
    for pgid in "${OWNER_PGIDS[@]:-}"; do
      if [[ -n "$pgid" ]] && kill -0 "-$pgid" >/dev/null 2>&1; then
        alive=1
      fi
    done
    for pid in "${OWNER_PIDS[@]:-}"; do
      if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
        alive=1
      fi
    done
    (( alive == 0 )) && break
    sleep 0.25
  done
  for index in "${!OWNER_PGIDS[@]}"; do
    pgid="${OWNER_PGIDS[$index]}"
    owner_pid="${OWNER_PGID_PIDS[$index]}"
    expected="${OWNER_PGID_STARTS[$index]}"
    if owned_identity_state "$owner_pid" "$expected"; then
      if kill -0 "-$pgid" >/dev/null 2>&1; then
        kill -KILL "-$pgid" >/dev/null 2>&1 || true
      fi
    else
      identity_status=$?
      if (( identity_status == 2 )); then
        echo "refusing to kill reused iOS proof process-group leader: $owner_pid" >&2
        cleanup_failed=1
      fi
    fi
  done
  for index in "${!OWNER_PIDS[@]}"; do
    pid="${OWNER_PIDS[$index]}"
    expected="${OWNER_STARTS[$index]}"
    if owned_identity_state "$pid" "$expected"; then
      if kill -0 "$pid" >/dev/null 2>&1; then
        kill -KILL "$pid" >/dev/null 2>&1 || true
      fi
    else
      identity_status=$?
      if (( identity_status == 2 )); then
        echo "refusing to kill reused iOS proof process: $pid" >&2
        cleanup_failed=1
      fi
    fi
  done
  for pid in "${OWNER_PIDS[@]:-}"; do
    [[ -n "$pid" ]] && wait "$pid" >/dev/null 2>&1 || true
  done
  for pgid in "${OWNER_PGIDS[@]:-}"; do
    if [[ -n "$pgid" ]] && kill -0 "-$pgid" >/dev/null 2>&1; then
      echo "owned iOS proof process group survived bounded teardown: $pgid" >&2
      cleanup_failed=1
    fi
  done
  for pid in "${OWNER_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      echo "owned iOS proof process survived bounded teardown: $pid" >&2
      cleanup_failed=1
    fi
  done

  if [[ -n "$SIM_UDID" ]]; then
    xcrun simctl shutdown "$SIM_UDID" >/dev/null 2>&1 || true
    xcrun simctl delete "$SIM_UDID" >/dev/null 2>&1 || true
    xcrun simctl list devices -j 2>/dev/null | python3 -c '
import json
import sys
try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(2)
needle = sys.argv[1]
exists = any(
    device.get("udid") == needle
    for devices in payload.get("devices", {}).values()
    for device in devices
)
raise SystemExit(0 if exists else 1)
' "$SIM_UDID"
    verify_status=$?
    if (( verify_status == 0 )); then
      echo "owned iOS proof simulator survived deletion: $SIM_UDID" >&2
      cleanup_failed=1
    elif (( verify_status != 1 )); then
      echo "could not verify deletion of owned iOS proof simulator: $SIM_UDID" >&2
      cleanup_failed=1
    fi
  fi

  if (( cleanup_failed )); then
    status=1
  fi
  echo "Live iOS control proof evidence: $OUT" >&2
  exit "$status"
}
trap cleanup EXIT INT TERM

read -r DEVICE_TYPE RUNTIME < <(python3 - <<'PY'
import json
import os
import subprocess
runtimes = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "runtimes", "available", "-j"], text=True))["runtimes"]
runtimes = [item for item in runtimes if "iOS" in item.get("identifier", "") and item.get("isAvailable")]
requested = os.environ.get("IOS_SIMULATOR_RUNTIME_VERSION")
if requested:
    runtime = next((item for item in runtimes if item.get("version") == requested), None)
    if runtime is None:
        raise SystemExit(f"requested iOS simulator runtime is not available: {requested}")
else:
    runtime = sorted(runtimes, key=lambda item: item.get("version", ""))[-1]
phones = [item for item in runtime.get("supportedDeviceTypes", []) if item.get("productFamily") == "iPhone"]
if not phones:
    raise SystemExit("no iPhone type supported by selected iOS runtime")
print(phones[0]["identifier"], runtime["identifier"])
PY
)
SIM_UDID="$(xcrun simctl create "Longhouse OMP Control Proof ${LONGHOUSE_LIVE_CONTROL_SESSION_ID:0:8}" "$DEVICE_TYPE" "$RUNTIME")"
xcrun simctl boot "$SIM_UDID"
xcrun simctl bootstatus "$SIM_UDID" -b
xcrun simctl addmedia "$SIM_UDID" "$IOS_LIVE_CONTROL_PHOTO"

# xcodebuild strips TEST_RUNNER_ and injects the remainder into the app/test
# runner. Keep credentials in process environment only; never write them into
# xctestrun files or evidence.
export TEST_RUNNER_LONGHOUSE_LIVE_CONTROL_REQUIRED=1
export TEST_RUNNER_LONGHOUSE_LIVE_CONTROL_SERVER_URL="$LONGHOUSE_LIVE_CONTROL_SERVER_URL"
export TEST_RUNNER_LONGHOUSE_LIVE_CONTROL_AUTH_TOKEN="$LONGHOUSE_LIVE_CONTROL_AUTH_TOKEN"
export TEST_RUNNER_LONGHOUSE_LIVE_CONTROL_SESSION_ID="$LONGHOUSE_LIVE_CONTROL_SESSION_ID"
for suffix in MARKER_PREFIX THINK_PROMPT PHOTO_PROMPT; do
  key="LONGHOUSE_LIVE_CONTROL_${suffix}"
  if [[ -n "${!key:-}" ]]; then
    export "TEST_RUNNER_${key}=${!key}"
  fi
done

make -C "$ROOT" ios-project
DESTINATION="platform=iOS Simulator,id=$SIM_UDID"
start_owned "$OUT/build.log" \
  xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
    -derivedDataPath "$DERIVED_DATA_PATH" build-for-testing
BUILD_PID="$LAST_PID"
if wait "$BUILD_PID"; then
  BUILD_STATUS=0
else
  BUILD_STATUS=$?
fi
if [[ "$BUILD_STATUS" -ne 0 ]]; then
  echo "iOS live control proof build failed (exit $BUILD_STATUS)" >&2
  exit "$BUILD_STATUS"
fi
start_owned "$OUT/test.log" \
  xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
    -derivedDataPath "$DERIVED_DATA_PATH" -resultBundlePath "$OUT/result.xcresult" \
    -collect-test-diagnostics never \
    -only-testing:"LonghouseIOSUITests/$TEST" test-without-building
TEST_PID="$LAST_PID"
if wait "$TEST_PID"; then
  STATUS=0
else
  STATUS=$?
fi

SUMMARY="$OUT/test-summary.json"
if [[ ! -d "$OUT/result.xcresult" ]]; then
  echo "iOS live control proof produced no xcresult bundle" >&2
  STATUS=1
elif ! xcrun xcresulttool get test-results summary --path "$OUT/result.xcresult" --format json >"$SUMMARY"; then
  echo "iOS live control proof could not read xcresult test summary" >&2
  STATUS=1
elif ! python3 - "$SUMMARY" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as stream:
    payload = json.load(stream)

COUNT_KEYS = {"testscount", "testcount", "testsran", "executedtests"}
SKIP_KEYS = {"testsskipped", "skippedtests", "skippedtestcount"}
STATUS_KEYS = {"teststatus", "status", "result"}


def metric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, dict)):
        return len(value)
    return None


def first_metric(value, keys):
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in keys:
                found = metric(child)
                if found is not None:
                    return found
        for child in value.values():
            found = first_metric(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_metric(child, keys)
            if found is not None:
                return found
    return None


def has_skipped_status(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in STATUS_KEYS and str(child).strip().lower() in {
                "skip",
                "skipped",
                "notrun",
                "not run",
            }:
                return True
            if has_skipped_status(child):
                return True
    elif isinstance(value, list):
        return any(has_skipped_status(child) for child in value)
    return False


count = first_metric(payload, COUNT_KEYS)
skipped = first_metric(payload, SKIP_KEYS)
if count is None or count <= 0:
    raise SystemExit(f"xcresult reports no executed live-control test (count={count!r})")
if skipped is not None and skipped > 0:
    raise SystemExit(f"xcresult reports skipped live-control tests (skipped={skipped})")
if has_skipped_status(payload):
    raise SystemExit("xcresult reports a skipped or not-run live-control test")
PY
then
  STATUS=1
fi

xcrun xcresulttool export attachments --path "$OUT/result.xcresult" --output-path "$OUT/attachments" >/dev/null 2>&1 || true

printf 'result_bundle=%s\n' "$OUT/result.xcresult"
exit "$STATUS"
