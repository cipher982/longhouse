#!/usr/bin/env bash
# Measure scroll-up waits on a simulator: a read-only UI test opens the top
# sessions of a simlab Runtime Host and flicks back through their history,
# and the app logs how long each flick sat at the top of what was loaded
# (`history_wall`) plus each older page it applied. Prints a summary.
#
#   scripts/ops/ios_scroll_up.sh [label]
#
# Needs a seeded simlab Runtime Host first, on the bench:
#   scripts/ops/bench.sh run 'python3 scripts/qa/simlab.py up --build --seed-corpus ~/bench/corpus/home \
#     && scripts/ops/ios_scroll_up.sh before; python3 scripts/qa/simlab.py down'
#
# Environment:
#   SCROLL_UP_SESSIONS  sessions to open from the top of the timeline (default 3)
#   SCROLL_UP_FLICKS    flicks toward older messages per session (default 16)
#   SCROLL_UP_OUT_DIR   output directory (default: $BENCH_OUT or /tmp/agents/ios-scroll-up)
#   SIM_UDID            pick the simulator
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="$ROOT_DIR/ios/XcodeHarness/LonghouseIOS.xcodeproj"
STATE="$ROOT_DIR/artifacts/simlab/current/simlab.json"
DERIVED="$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-ScrollUp"
OUT_DIR="${SCROLL_UP_OUT_DIR:-${BENCH_OUT:-/tmp/agents/ios-scroll-up}}"
LABEL="${1:-scroll-up}"

die() { echo "ios-scroll-up: $*" >&2; exit 1; }

booted_here=""
cleanup() {
  if [[ -n "$booted_here" ]]; then
    xcrun simctl shutdown "$booted_here" 2>/dev/null || true
    booted_here=""
  fi
}
trap cleanup EXIT

[[ -f "$STATE" ]] || die "no simlab run; start one with simlab.py up --seed-corpus <dir>"
read -r url token < <(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s.get("client_url") or s["base_url"], s["token"])' "$STATE")
DEVICE="${SIM_UDID:-$(python3 "$ROOT_DIR/scripts/ci/select_ios_simulator.py" "$PROJECT" LonghouseChatStress | sed -n 's/.*id=//p')}"
[[ -n "$DEVICE" ]] || die "no simulator"
if ! xcrun simctl list devices booted | grep -q "$DEVICE"; then
  xcrun simctl boot "$DEVICE"
  booted_here="$DEVICE"
fi
xcrun simctl bootstatus "$DEVICE" -b >/dev/null
# Every run starts from a cold app: a transcript cache left by an earlier run
# would answer the history this run is meant to time.
xcrun simctl uninstall "$DEVICE" ai.longhouse.ios >/dev/null 2>&1 || true
# Keep the app's debug lines (every mobile-tail request's size and time).
xcrun simctl spawn "$DEVICE" log config --subsystem ai.longhouse.ios --mode level:debug,persist:debug >/dev/null 2>&1 || true

mkdir -p "$OUT_DIR"
BASE="$OUT_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$LABEL"
started=$SECONDS
(cd "$ROOT_DIR" && make ios-project >/dev/null)
# Optimized like the tour: unoptimized Swift on a simulator spends seconds in
# runtime metadata lookups the shipping app never pays.
if ! xcodebuild -project "$PROJECT" -scheme LonghouseChatStress -configuration Debug \
  -destination "platform=iOS Simulator,id=$DEVICE" -derivedDataPath "$DERIVED" \
  SWIFT_OPTIMIZATION_LEVEL=-O SWIFT_COMPILATION_MODE=wholemodule GCC_OPTIMIZATION_LEVEL=s ENABLE_DEBUG_DYLIB=NO \
  build-for-testing > "$BASE-build.log" 2>&1; then
  grep -E "error:" "$BASE-build.log" | head -10 >&2
  die "build failed; log at $BASE-build.log"
fi
echo "build: $((SECONDS - started))s" >&2

test_started=$SECONDS
log_start="$(date '+%Y-%m-%d %H:%M:%S')"
test_status=0
env TEST_RUNNER_LONGHOUSE_RUN_LIVE_SCROLL_UP=1 \
  "TEST_RUNNER_LONGHOUSE_SCROLL_UP_SESSIONS=${SCROLL_UP_SESSIONS:-3}" \
  "TEST_RUNNER_LONGHOUSE_SCROLL_UP_FLICKS=${SCROLL_UP_FLICKS:-16}" \
  "TEST_RUNNER_LONGHOUSE_HEADLESS_SERVER_URL=$url" \
  "TEST_RUNNER_LONGHOUSE_HEADLESS_AUTH_TOKEN=$token" \
  xcodebuild -project "$PROJECT" -scheme LonghouseChatStress -configuration Debug \
  -destination "platform=iOS Simulator,id=$DEVICE" -derivedDataPath "$DERIVED" \
  -resultBundlePath "$BASE.xcresult" \
  -only-testing:LonghouseChatStressUITests/SessionOpenPerformanceUITests/testLiveScrollUpHistory \
  test-without-building > "$BASE-test.log" 2>&1 || test_status=$?
echo "scroll-up run: $((SECONDS - test_started))s (status $test_status)" >&2

xcrun simctl spawn "$DEVICE" log show --start "$log_start" --info --debug --style compact \
  --predicate 'subsystem == "ai.longhouse.ios" AND (category == "TranscriptScroll" OR category == "SessionOpen" OR category == "LonghouseAPI")' \
  > "$BASE-app.log" 2>/dev/null || true
xcrun simctl terminate "$DEVICE" ai.longhouse.ios >/dev/null 2>&1 || true
cleanup

python3 - "$BASE-app.log" "$BASE-test.log" <<'PY'
import re, statistics, sys
app_log, test_log = sys.argv[1], sys.argv[2]
text = open(app_log, errors="replace").read()
released = [int(m) for m in re.findall(r"history_wall outcome=released wait_ms=(\d+)", text)]
left = [int(m) for m in re.findall(r"history_wall outcome=left wait_ms=(\d+)", text)]
older = re.findall(r"stage=older_applied .*?page_items=(\d+) added=(\S+)", text)
bytes_ = [int(m) for m in re.findall(r"mobile-tail response received .*?decoded_bytes=(\d+)", text)]
elapsed = [int(m) for m in re.findall(r"mobile-tail response received .*?elapsed_ms=(\d+)", text)]
def pct(values, q):
    if not values: return "-"
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]
print(f"history walls released: {len(released)}  p50={pct(released, .5)}ms p95={pct(released, .95)}ms max={max(released) if released else '-'}ms total={sum(released)}ms")
print(f"history walls left (gave up or session start): {len(left)}  p50={pct(left, .5)}ms")
print(f"older pages applied: {len(older)}")
if bytes_:
    print(f"mobile-tail responses: {len(bytes_)}  median={int(statistics.median(bytes_))}B  total={sum(bytes_)}B")
if elapsed:
    print(f"mobile-tail request time: p50={pct(elapsed, .5)}ms p95={pct(elapsed, .95)}ms")
metric = re.search(r"IOS_LIVE_SCROLL_UP_METRIC.*", open(test_log, errors="replace").read())
print(metric.group(0) if metric else "no IOS_LIVE_SCROLL_UP_METRIC line (test failed?)")
PY
echo "logs: $BASE-app.log $BASE-test.log" >&2
exit "$test_status"
