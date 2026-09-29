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
# IOS_TEST_SLICE=i/n runs every n-th test of each scheme, starting at the i-th, so
# n VMs can split one scheme's tests (scripts/ci/ios_test_slice.py). Unset runs
# all of them.
IOS_TEST_SLICE="${IOS_TEST_SLICE:-}"

mkdir -p "${DERIVED_DATA_PATH}"

# Elapsed time at each stage: this lane's minutes go to a few long phases (build,
# simulator boot, the UI tests) and the job log shows none of them by name.
stage() { echo "[run_ios_tests] $* (t+${SECONDS}s)"; }

# One -only-testing argument per line for the tests of $1 that this slice owns.
# Any failure aborts the run: an empty answer would otherwise read as "nothing
# to run" and pass.
slice_arguments() {
  local scheme="$1" directory enumeration status=0
  # xcodebuild refuses to overwrite its output path, so hand it a name in a fresh
  # directory rather than a mktemp file.
  directory="$(mktemp -d)"
  enumeration="${directory}/tests.json"
  # errexit is off inside the command substitution this runs in, so the status is
  # carried by hand; a bare `rm` last would turn a failed helper into success.
  xcodebuild \
    -project "${PROJECT_PATH}" \
    -scheme "${scheme}" \
    -destination "${DESTINATION}" \
    -derivedDataPath "${DERIVED_DATA_PATH}" \
    -enumerate-tests \
    -test-enumeration-style flat \
    -test-enumeration-format json \
    -test-enumeration-output-path "${enumeration}" \
    test-without-building >/dev/null || status=$?
  if [[ "${status}" -eq 0 ]]; then
    python3 "$(dirname "${BASH_SOURCE[0]}")/ios_test_slice.py" "${enumeration}" "${IOS_TEST_SLICE}" || status=$?
  fi
  rm -f "${enumeration}"
  rmdir "${directory}"
  return "${status}"
}

run_scheme() {
  local scheme="$1"
  local result_bundle=""
  local only_testing=()
  local slice_output

  if [[ -n "${RESULTS_DIR}" ]]; then
    mkdir -p "${RESULTS_DIR}"
    result_bundle="${RESULTS_DIR}/${scheme}.xcresult"
    rm -rf "${result_bundle}"
  fi

  stage "${scheme}: build-for-testing"
  xcodebuild \
    -project "${PROJECT_PATH}" \
    -scheme "${scheme}" \
    -destination "${DESTINATION}" \
    -derivedDataPath "${DERIVED_DATA_PATH}" \
    -disableAutomaticPackageResolution \
    build-for-testing

  if [[ -n "${IOS_TEST_SLICE}" ]]; then
    stage "${scheme}: choosing the tests of slice ${IOS_TEST_SLICE}"
    slice_output="$(slice_arguments "${scheme}")"
    while IFS= read -r argument; do
      if [[ -n "${argument}" ]]; then
        only_testing+=("${argument}")
      fi
    done <<<"${slice_output}"
    if [[ ${#only_testing[@]} -eq 0 ]]; then
      stage "${scheme}: slice ${IOS_TEST_SLICE} owns no tests"
      return 0
    fi
    stage "${scheme}: slice ${IOS_TEST_SLICE} runs ${#only_testing[@]} tests"
  fi

  stage "${scheme}: test-without-building"
  if [[ -n "${result_bundle}" ]]; then
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      -resultBundlePath "${result_bundle}" \
      ${only_testing[@]+"${only_testing[@]}"} \
      test-without-building
  else
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      ${only_testing[@]+"${only_testing[@]}"} \
      test-without-building
  fi
}

echo "Running iOS schemes: ${IOS_TEST_SCHEMES}"
for scheme in ${IOS_TEST_SCHEMES}; do
  run_scheme "${scheme}"
  stage "${scheme}: done"
done
