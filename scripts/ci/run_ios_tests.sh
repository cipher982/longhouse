#!/usr/bin/env bash
set -euo pipefail

if ! python3 "$(dirname "${BASH_SOURCE[0]}")/../qa/test_boundary.py"; then
  echo "Native tests run in a disposable hosted macOS VM. Use make test-ios." >&2
  exit 2
fi

PROJECT_PATH="${PROJECT_PATH:-ios/XcodeHarness/LonghouseIOS.xcodeproj}"
DESTINATION="${1:-${IOS_DESTINATION:-}}"

if [[ -z "${DESTINATION}" ]]; then
  echo "usage: run_ios_tests.sh <destination>" >&2
  echo "or set IOS_DESTINATION=platform=iOS Simulator,OS=...,name=..." >&2
  exit 2
fi

DERIVED_DATA_PATH="${IOS_DERIVED_DATA_PATH:-${HOME}/Library/Developer/Xcode/DerivedData/LonghouseIOS-CI}"
RESULTS_DIR="${IOS_RESULTS_DIR:-}"
IOS_TEST_SCHEMES="${IOS_TEST_SCHEMES:-Longhouse}"

mkdir -p "${DERIVED_DATA_PATH}"

run_scheme() {
  local scheme="$1"
  local result_bundle=""

  if [[ -n "${RESULTS_DIR}" ]]; then
    mkdir -p "${RESULTS_DIR}"
    result_bundle="${RESULTS_DIR}/${scheme}.xcresult"
    rm -rf "${result_bundle}"
  fi

  xcodebuild \
    -project "${PROJECT_PATH}" \
    -scheme "${scheme}" \
    -destination "${DESTINATION}" \
    -derivedDataPath "${DERIVED_DATA_PATH}" \
    build-for-testing

  if [[ -n "${simulator_id:-}" ]]; then
    local wait_started=${SECONDS}
    xcrun simctl bootstatus "${simulator_id}" -b >/dev/null
    echo "[ios] simulator ready; waited $((SECONDS - wait_started))s after the build"
  fi

  if [[ -n "${result_bundle}" ]]; then
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      -resultBundlePath "${result_bundle}" \
      test-without-building
  else
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      test-without-building
  fi
}

# A cold hosted simulator takes minutes to boot, and xcodebuild otherwise boots
# it only after the build. Boot it now, in parallel with the build.
simulator_id=""
if [[ "${DESTINATION}" =~ id=([0-9A-Fa-f-]+) ]]; then
  simulator_id="${BASH_REMATCH[1]}"
  (xcrun simctl boot "${simulator_id}" >/dev/null 2>&1 || true) &
fi

started=${SECONDS}
echo "Running iOS schemes: ${IOS_TEST_SCHEMES}"
for scheme in ${IOS_TEST_SCHEMES}; do
  scheme_started=${SECONDS}
  run_scheme "${scheme}"
  echo "[ios] ${scheme}: $((SECONDS - scheme_started))s"
done
echo "[ios] total $((SECONDS - started))s"
