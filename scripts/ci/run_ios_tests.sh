#!/usr/bin/env bash
set -euo pipefail

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

echo "Running iOS schemes: ${IOS_TEST_SCHEMES}"
for scheme in ${IOS_TEST_SCHEMES}; do
  run_scheme "${scheme}"
done
