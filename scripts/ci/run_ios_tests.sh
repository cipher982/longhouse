#!/usr/bin/env bash
set -euo pipefail

if ! python3 "$(dirname "${BASH_SOURCE[0]}")/../qa/test_boundary.py"; then
  echo "Native tests run on the wisp runner. Use make test-ios." >&2
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
# Every xcodebuild below passes -enableCodeCoverage NO. The smoke scheme's test plan
# turned coverage on and the unit scheme did not, so the two schemes built the app
# and widget with different compiler arguments, and each build threw away the
# other's incremental state: every Swift file compiled twice per job (run
# 45691666834). Nothing reads the coverage.
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

# IOS_PARALLEL_WORKERS simulator clones run the classes of each scheme named in
# IOS_PARALLEL_SCHEMES side by side (xcodebuild hands each clone whole classes).
# Unset, or 1, runs every scheme on the one simulator.
parallel_arguments() {
  case " ${IOS_PARALLEL_SCHEMES:-} " in
    *" $1 "*)
      if [[ "${IOS_PARALLEL_WORKERS:-1}" -gt 1 ]]; then
        printf '%s\n' -parallel-testing-enabled YES -parallel-testing-worker-count "${IOS_PARALLEL_WORKERS}"
      fi
      ;;
  esac
}

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

build_for_testing() {
  local settings=()
  # With kept derived data, have the Swift driver say why each file is compiled
  # again, so a build that stops being incremental shows its cause in the log.
  if [[ "${IOS_DERIVED_DATA_KEPT:-}" == "1" ]]; then
    settings+=('OTHER_SWIFT_FLAGS=$(inherited) -driver-show-incremental')
  fi
  xcodebuild \
    -project "${PROJECT_PATH}" \
    -scheme "$1" \
    -destination "${DESTINATION}" \
    -derivedDataPath "${DERIVED_DATA_PATH}" \
    -enableCodeCoverage NO \
    -disableAutomaticPackageResolution \
    ${settings[@]+"${settings[@]}"} \
    build-for-testing
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
  while IFS= read -r argument; do
    if [[ -n "${argument}" ]]; then
      filters+=("${argument}")
    fi
  done < <(parallel_arguments "${scheme}")

  stage "${scheme}: build-for-testing"
  if ! build_for_testing "${scheme}"; then
    # Derived data kept from earlier jobs (IOS_DERIVED_DATA_KEPT, the bench runner) can hold
    # state that breaks a build no clean checkout would. Build once more from nothing; a real
    # compile error fails again, and the clean output is what the next job starts from.
    if [[ "${IOS_DERIVED_DATA_KEPT:-}" != "1" ]]; then
      return 1
    fi
    echo "::warning::${scheme}: build failed in kept derived data; building again from nothing"
    stage "${scheme}: build-for-testing, clean"
    # SourcePackages stays: the packages were resolved before this script, and it builds with
    # automatic resolution off.
    find "${DERIVED_DATA_PATH:?}" -mindepth 1 -maxdepth 1 ! -name SourcePackages -exec rm -rf {} +
    build_for_testing "${scheme}"
  fi

  stage "${scheme}: test-without-building ${filters[*]-}"
  if [[ -n "${result_bundle}" ]]; then
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      -enableCodeCoverage NO \
      -resultBundlePath "${result_bundle}" \
      ${filters[@]+"${filters[@]}"} \
      test-without-building
  else
    xcodebuild \
      -project "${PROJECT_PATH}" \
      -scheme "${scheme}" \
      -destination "${DESTINATION}" \
      -derivedDataPath "${DERIVED_DATA_PATH}" \
      -enableCodeCoverage NO \
      ${filters[@]+"${filters[@]}"} \
      test-without-building
  fi
}

echo "Running iOS schemes: ${IOS_TEST_SCHEMES}"
for scheme in ${IOS_TEST_SCHEMES}; do
  run_scheme "${scheme}"
  stage "${scheme}: done"
done
