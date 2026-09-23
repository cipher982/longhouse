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
#   TOUR_OPTIMIZED      1 (default): compile Debug with -O, whole-module, no
#                       debug dylib. The test hooks (headless sign-in) are
#                       DEBUG-only, and unoptimized Swift on a simulator spends
#                       seconds in runtime metadata lookups the shipping app
#                       never pays, which buried the app's own hotspots.
#   TOUR_OUT_DIR        output directory (default: /tmp/agents/ios-tour)
#   SIM_UDID / PHONE_DEVICE  pick the simulator or phone
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="$ROOT_DIR/ios/XcodeHarness/LonghouseIOS.xcodeproj"
TARGET="${TOUR_TARGET:-simulator}"
CONFIGURATION="${TOUR_CONFIGURATION:-Debug}"
declare -a optimize=()
FLAVOR="$CONFIGURATION"
if [[ "${TOUR_OPTIMIZED:-1}" == 1 ]]; then
  optimize=(SWIFT_OPTIMIZATION_LEVEL=-O SWIFT_COMPILATION_MODE=wholemodule GCC_OPTIMIZATION_LEVEL=s ENABLE_DEBUG_DYLIB=NO)
  FLAVOR="$CONFIGURATION-O"
fi
DERIVED="$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-Tour-$TARGET-$FLAVOR"
OUT_DIR="${TOUR_OUT_DIR:-/tmp/agents/ios-tour}"
LABEL="${1:-tour}"
STATE="$ROOT_DIR/artifacts/simlab/current/simlab.json"

die() { echo "ios-tour: $*" >&2; exit 1; }

booted_here=""
recorder=""
tester=""
cleanup() {
  if [[ -n "$tester" ]] && kill -0 "$tester" 2>/dev/null; then
    kill "$tester" 2>/dev/null || true
  fi
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
# TOUR_SWIFTUI=1 adds view-body counts; on the 8 GB bench it slows the tour
# enough to distort it and breaks symbolication, so it is opt-in.
[[ "${TOUR_SWIFTUI:-0}" == 1 ]] && instruments+=(--instrument SwiftUI)
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
BASE="$OUT_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$LABEL-$TARGET-$FLAVOR"
started=$SECONDS
(cd "$ROOT_DIR" && make ios-project >/dev/null)
if ! xcodebuild -project "$PROJECT" -scheme LonghouseChatStress -configuration "$CONFIGURATION" \
  -destination "$destination" -derivedDataPath "$DERIVED" ${signing[@]+"${signing[@]}"} \
  ${optimize[@]+"${optimize[@]}"} DEBUG_INFORMATION_FORMAT=dwarf-with-dsym build-for-testing > "$BASE-build.log" 2>&1; then
  grep -E "error:" "$BASE-build.log" | head -10 >&2
  die "build failed; log at $BASE-build.log"
fi
echo "build: $((SECONDS - started))s" >&2

app_pid() {
  if [[ "$TARGET" == simulator ]]; then
    xcrun simctl spawn "$DEVICE" launchctl list 2>/dev/null \
      | awk '/UIKitApplication:ai\.longhouse\.ios\[/ && $1 ~ /^[0-9]+$/ {print $1; exit}'
  else
    xcrun devicectl device info processes --device "$DEVICE" --json-output - 2>/dev/null | python3 -c '
import json, sys
for p in json.load(sys.stdin)["result"]["runningProcesses"]:
    if p.get("executable", "").endswith("/Longhouse.app/Longhouse"):
        print(p["processIdentifier"]); break
'
  fi
}

# The tour, recorded attached to the app alone: sampling every process in a
# simulator at 1 kHz starved the 8 GB bench until SpringBoard and the test
# runner hung for seconds, which then read as app slowness.
trace="$BASE.trace"
test_started=$SECONDS
log_start="$(date '+%Y-%m-%d %H:%M:%S')"
env "${runner_env[@]}" xcodebuild -project "$PROJECT" -scheme LonghouseChatStress \
  -configuration "$CONFIGURATION" -destination "$destination" -derivedDataPath "$DERIVED" \
  -resultBundlePath "$BASE.xcresult" \
  -only-testing:LonghouseChatStressUITests/SessionOpenPerformanceUITests/testLiveDogfoodTour \
  test-without-building > "$BASE-test.log" 2>&1 &
tester=$!
pid=""
for _ in $(seq 1 240); do
  pid="$(app_pid)"
  [[ -n "$pid" ]] && break
  kill -0 "$tester" 2>/dev/null || break
  sleep 0.5
done
if [[ -n "$pid" ]]; then
  xcrun xctrace record --device "$DEVICE" --attach "$pid" --no-prompt \
    --template "Time Profiler" "${instruments[@]}" \
    --time-limit 600s --output "$trace" > "$BASE-xctrace.log" 2>&1 &
  recorder=$!
fi
test_status=0
wait "$tester" || test_status=$?
echo "tour: $((SECONDS - test_started))s" >&2
if [[ -n "$recorder" ]]; then
  kill -INT "$recorder" 2>/dev/null || true
  wait "$recorder" 2>/dev/null || true
  recorder=""
fi

# Cold launch on its own, under the App Launch template, which starts the
# app itself: process start, first frame, and the first seconds of work.
launch_trace="$BASE-launch.trace"
if [[ "$TARGET" == simulator ]]; then
  xcrun simctl terminate "$DEVICE" ai.longhouse.ios >/dev/null 2>&1 || true
elif pid="$(app_pid)" && [[ -n "$pid" ]]; then
  xcrun devicectl device process terminate --device "$DEVICE" --pid "$pid" >/dev/null 2>&1 || true
fi
sleep 2
declare -a launch_env=()
if [[ "$TARGET" == simulator ]]; then
  launch_env=(--env "LONGHOUSE_HEADLESS_SERVER_URL=$url" --env "LONGHOUSE_HEADLESS_AUTH_TOKEN=$token")
fi
xcrun xctrace record --device "$DEVICE" --template "App Launch" --no-prompt --time-limit 15s \
  ${launch_env[@]+"${launch_env[@]}"} --output "$launch_trace" --launch -- ai.longhouse.ios \
  > "$BASE-launch-xctrace.log" 2>&1 || true

if [[ "$TARGET" == simulator ]]; then
  # The app's own milestones, which time the work without XCUITest's
  # synthesis and idle waits. (On a device the os_log instrument has them.)
  xcrun simctl spawn "$DEVICE" log show --start "$log_start" --info --style compact \
    --predicate 'subsystem == "ai.longhouse.ios"' > "$BASE-app.log" 2>/dev/null || true
fi
cleanup

# Debug builds keep symbols in object files; the dSYMs built above let
# Instruments name the app's frames.
# symbolicate wants the .dSYM itself and a separate output trace; handed
# the Products directory, or asked to rewrite in place, it reports success
# and changes nothing.
dsym="$(ls -d "$DERIVED"/Build/Products/*-iphone*/Longhouse.app.dSYM 2>/dev/null | head -1)"
if [[ -n "$dsym" ]]; then
  for t in "$trace" "$launch_trace"; do
    [[ -d "$t" ]] || continue
    if xcrun xctrace symbolicate --input "$t" --dsym "$dsym" --output "${t%.trace}-sym.trace" > /dev/null 2>&1; then
      [[ "$t" == "$trace" ]] && trace="${t%.trace}-sym.trace"
      [[ "$t" == "$launch_trace" ]] && launch_trace="${t%.trace}-sym.trace"
    fi
  done
fi

grep -E "IOS_LIVE_TOUR_METRIC|error:|XCTSkip|skipped" "$BASE-test.log" | head -10 || true
[[ "$test_status" == 0 ]] || die "tour failed (status $test_status); log at $BASE-test.log"
if [[ -s "$BASE-app.log" ]]; then
  echo "app milestones ($BASE-app.log):"
  grep -E "timeline (first paint|cache (hit|miss)|refresh finished)|session open stage=(start|request_start|detail_request_start|stop|history_fill)|stall" "$BASE-app.log" \
    | sed -E 's/^([0-9-]+ )?([0-9:.]+).*\[([A-Za-z]+)\] /\2 \3 /' | cut -c1-170 | head -60
fi
if [[ -d "$launch_trace" ]]; then
  echo; echo "== cold launch: $launch_trace"
  python3 "$ROOT_DIR/scripts/ops/trace_summary.py" "$launch_trace" --process Longhouse --top 15 ${dsym:+--dsym "$dsym"}
fi
if [[ -d "$trace" ]]; then
  echo; echo "== tour: $trace"
  python3 "$ROOT_DIR/scripts/ops/trace_summary.py" "$trace" --process Longhouse ${dsym:+--dsym "$dsym"}
else
  echo "no tour trace: the app process never appeared (see $BASE-test.log)"
fi
