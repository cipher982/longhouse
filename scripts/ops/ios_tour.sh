#!/usr/bin/env bash
# Record Instruments while a read-only UI test tours the app the way a person
# uses it: cold launch, timeline scroll, open and scroll the top sessions,
# background and resume. Prints the tour's timings and a one-page summary of
# the trace (CPU by thread, app frames, hangs, hitches).
#
#   scripts/ops/ios_tour.sh [label]
#
# Targets:
#   simulator (default)  signs in to the running simlab Runtime Host; seed it
#                        with real transcripts first:
#                        simlab.py up --build --seed-corpus ~/bench/corpus/home
#   device               a paired test iPhone (never David's own), using the
#                        installed app's sign-in; the phone must be unlocked
#
# Run it on the bench: scripts/ops/bench.sh run scripts/ops/ios_tour.sh
#
# Environment:
#   TOUR_TARGET         simulator or device
#   TOUR_CONFIGURATION  Debug (default) or Release
#   TOUR_OUT_DIR        output directory (default: /tmp/agents/ios-tour)
#   SIM_UDID / PHONE_DEVICE  pick the simulator or phone
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="$ROOT_DIR/ios/XcodeHarness/LonghouseIOS.xcodeproj"
TARGET="${TOUR_TARGET:-simulator}"
CONFIGURATION="${TOUR_CONFIGURATION:-Debug}"
DERIVED="$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-Tour-$TARGET-$CONFIGURATION"
OUT_DIR="${TOUR_OUT_DIR:-/tmp/agents/ios-tour}"
LABEL="${1:-tour}"
STATE="$ROOT_DIR/artifacts/simlab/current/simlab.json"

die() { echo "ios-tour: $*" >&2; exit 1; }

booted_here=""
recorder=""
cleanup() {
  if [[ -n "$recorder" ]]; then
    kill -INT "$recorder" 2>/dev/null || true
    wait "$recorder" 2>/dev/null || true
    recorder=""
  fi
  if [[ -n "$booted_here" ]]; then
    xcrun simctl shutdown "$booted_here" 2>/dev/null || true
    booted_here=""
  fi
}
trap cleanup EXIT

declare -a runner_env=(TEST_RUNNER_LONGHOUSE_RUN_LIVE_TOUR=1)
declare -a signing=()
# Hitches is device-only; asking for it on a simulator fails the whole
# recording, CPU samples included.
declare -a instruments=(--instrument os_log)
case "$TARGET" in
  simulator)
    [[ -f "$STATE" ]] || die "no simlab run; start one with simlab.py up --seed-corpus <dir>"
    read -r url token < <(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s.get("client_url") or s["base_url"], s["token"])' "$STATE")
    DEVICE="${SIM_UDID:-$(python3 "$ROOT_DIR/scripts/ci/select_ios_simulator.py" "$PROJECT" LonghouseChatStress | sed -n 's/.*id=//p')}"
    [[ -n "$DEVICE" ]] || die "no simulator"
    if ! xcrun simctl list devices booted | grep -q "$DEVICE"; then
      xcrun simctl boot "$DEVICE"
      booted_here="$DEVICE"
    fi
    xcrun simctl bootstatus "$DEVICE" -b >/dev/null
    destination="platform=iOS Simulator,id=$DEVICE"
    runner_env+=("TEST_RUNNER_LONGHOUSE_HEADLESS_SERVER_URL=$url" "TEST_RUNNER_LONGHOUSE_HEADLESS_AUTH_TOKEN=$token")
    ;;
  device)
    DEVICE="${PHONE_DEVICE:-$(xcrun devicectl list devices --json-output - 2>/dev/null | python3 -c '
import json, sys
devices = json.load(sys.stdin)["result"]["devices"]
phones = [d for d in devices
          if d.get("hardwareProperties", {}).get("reality") == "physical"
          and d.get("hardwareProperties", {}).get("platform") == "iOS"]
phones.sort(key=lambda d: d.get("connectionProperties", {}).get("tunnelState") != "connected")
print(phones[0]["hardwareProperties"]["udid"] if phones else "")
')}"
    [[ -n "$DEVICE" ]] || die "no paired iPhone"
    destination="id=$DEVICE"
    team="${PHONE_TEAM_ID:-$(security find-certificate -c "Apple Development" -p 2>/dev/null \
      | openssl x509 -noout -subject 2>/dev/null | sed -n 's/.*OU *= *\([A-Z0-9]*\).*/\1/p' | head -1)}"
    signing=(-allowProvisioningUpdates "DEVELOPMENT_TEAM=$team")
    instruments+=(--instrument Hitches)
    ;;
  *) die "TOUR_TARGET must be simulator or device" ;;
esac

mkdir -p "$OUT_DIR"
BASE="$OUT_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$LABEL-$TARGET-$CONFIGURATION"
started=$SECONDS
(cd "$ROOT_DIR" && make ios-project >/dev/null)
if ! xcodebuild -project "$PROJECT" -scheme LonghouseChatStress -configuration "$CONFIGURATION" \
  -destination "$destination" -derivedDataPath "$DERIVED" ${signing[@]+"${signing[@]}"} \
  build-for-testing > "$BASE-build.log" 2>&1; then
  grep -E "error:" "$BASE-build.log" | head -10 >&2
  die "build failed; log at $BASE-build.log"
fi
echo "build: $((SECONDS - started))s" >&2

trace="$BASE.trace"
xcrun xctrace record --device "$DEVICE" --all-processes --no-prompt \
  --template "Time Profiler" "${instruments[@]}" \
  --time-limit 600s --output "$trace" > "$BASE-xctrace.log" 2>&1 &
recorder=$!
sleep 5

test_started=$SECONDS
log_start="$(date '+%Y-%m-%d %H:%M:%S')"
status=0
env "${runner_env[@]}" xcodebuild -project "$PROJECT" -scheme LonghouseChatStress \
  -configuration "$CONFIGURATION" -destination "$destination" -derivedDataPath "$DERIVED" \
  -resultBundlePath "$BASE.xcresult" \
  -only-testing:LonghouseChatStressUITests/SessionOpenPerformanceUITests/testLiveDogfoodTour \
  test-without-building > "$BASE-test.log" 2>&1 || status=$?
echo "tour: $((SECONDS - test_started))s" >&2
if [[ "$TARGET" == simulator ]]; then
  # The app's own milestones, which time the work without XCUITest's
  # synthesis and idle waits. (On a device the os_log instrument has them.)
  xcrun simctl spawn "$DEVICE" log show --start "$log_start" --info --style compact \
    --predicate 'subsystem == "ai.longhouse.ios"' > "$BASE-app.log" 2>/dev/null || true
fi
cleanup

grep -E "IOS_LIVE_TOUR_METRIC|error:|XCTSkip|skipped" "$BASE-test.log" | head -10 || true
[[ "$status" == 0 ]] || die "tour failed (status $status); log at $BASE-test.log"
if [[ -s "$BASE-app.log" ]]; then
  echo "app milestones ($BASE-app.log):"
  grep -E "timeline (first paint|cache (hit|miss)|refresh finished)|session open stage=(start|request_start|detail_request_start|stop|history_fill)|stall" "$BASE-app.log" \
    | sed -E 's/^([0-9-]+ )?([0-9:.]+).*\[([A-Za-z]+)\] /\2 \3 /' | cut -c1-170 | head -60
fi
echo "trace: $trace"
python3 "$ROOT_DIR/scripts/ops/trace_summary.py" "$trace" --process Longhouse
