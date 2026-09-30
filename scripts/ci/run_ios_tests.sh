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
# IOS_TEST_FILTER splits one scheme's tests over VMs: space-separated entries
# `Scheme:only:Id` or `Scheme:skip:Id` (an -only-testing / -skip-testing target or
# class) that apply to that scheme's run. CI gives one lane `skip:X` and another
# `only:X`, so any test outside X runs in the first. Unset runs everything.
IOS_TEST_FILTER="${IOS_TEST_FILTER:-}"

mkdir -p "${DERIVED_DATA_PATH}"

# An entry that is malformed, or names a scheme this run does not build, would
# otherwise be ignored and the run would pass having tested more or less than the
# lane meant.
for entry in ${IOS_TEST_FILTER}; do
  entry_scheme="${entry%%:*}"
  entry_rest="${entry#*:}"
  case " ${IOS_TEST_SCHEMES} " in
    *" ${entry_scheme} "*) ;;
    *)
      echo "IOS_TEST_FILTER entry '${entry}' names a scheme that IOS_TEST_SCHEMES ('${IOS_TEST_SCHEMES}') does not run" >&2
      exit 2
      ;;
  esac
  case "${entry_rest}" in
    only:?* | skip:?*) ;;
    *)
      echo "IOS_TEST_FILTER entry '${entry}' must look like Scheme:only:Id or Scheme:skip:Id" >&2
      exit 2
      ;;
  esac
done

# Elapsed time at each stage: this lane's minutes go to a few long phases (build,
# simulator boot, the UI tests) and the job log shows none of them by name.
stage() { echo "[run_ios_tests] $* (t+${SECONDS}s)"; }

# One -only-testing / -skip-testing argument per line for the entries of $1.
filter_arguments() {
  local scheme="$1" entry rest
  for entry in ${IOS_TEST_FILTER}; do
    if [[ "${entry%%:*}" != "${scheme}" ]]; then
      continue
    fi
    rest="${entry#*:}"
    if [[ "${rest%%:*}" == "only" ]]; then
      printf '%s\n' "-only-testing:${rest#*:}"
    else
      printf '%s\n' "-skip-testing:${rest#*:}"
    fi
  done
}

run_scheme() {
  local scheme="$1"
  local result_bundle=""
  local filters=()
  local argument

  if [[ -n "${RESULTS_DIR}" ]]; then
    mkdir -p "${RESULTS_DIR}"
    result_bundle="${RESULTS_DIR}/${scheme}.xcresult"
    rm -rf "${result_bundle}"
  fi

  while IFS= read -r argument; do
    if [[ -n "${argument}" ]]; then
      filters+=("${argument}")
    fi
  done < <(filter_arguments "${scheme}")

  stage "${scheme}: build-for-testing"
  xcodebuild \
    -project "${PROJECT_PATH}" \
    -scheme "${scheme}" \
    -destination "${DESTINATION}" \
    -derivedDataPath "${DERIVED_DATA_PATH}" \
    -disableAutomaticPackageResolution \
    build-for-testing

  stage "${scheme}: test-without-building ${filters[*]-}"
  if [[ -n "${result_bundle}" ]]; then
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      -resultBundlePath "${result_bundle}" \
      ${filters[@]+"${filters[@]}"} \
      test-without-building
  else
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      ${filters[@]+"${filters[@]}"} \
      test-without-building
  fi
}

echo "Running iOS schemes: ${IOS_TEST_SCHEMES}"
for scheme in ${IOS_TEST_SCHEMES}; do
  run_scheme "${scheme}"
  stage "${scheme}: done"
done
