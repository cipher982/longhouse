#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-notarize-file.sh \
  --file <path> \
  --keychain-profile <profile> \
  [--keychain <path>] \
  [--staple <path>]... \
  [--submission-id <id>] \
  [--submission-id-file <path>] \
  [--submit-only] \
  [--timeout <duration>]

Submits a signed distribution file such as a DMG to Apple's notary service,
waits for acceptance unless --submit-only is used, and staples the accepted
ticket to any requested targets.
EOF
}

require_value() {
  local flag="$1"
  local value="${2:-}"
  if [[ -z "$value" ]]; then
    echo "Missing value for $flag" >&2
    usage >&2
    exit 1
  fi
}

FILE_PATH=""
KEYCHAIN_PROFILE=""
KEYCHAIN_PATH=""
SUBMISSION_ID=""
SUBMISSION_ID_FILE=""
SUBMIT_ONLY=0
WAIT_TIMEOUT="${LONGHOUSE_NOTARY_WAIT_TIMEOUT:-90m}"
STATUS_POLL_INTERVAL_SECONDS="${LONGHOUSE_NOTARY_POLL_INTERVAL_SECONDS:-60}"
STAPLE_TARGETS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)
      require_value "$1" "${2:-}"
      FILE_PATH="$2"
      shift 2
      ;;
    --keychain-profile)
      require_value "$1" "${2:-}"
      KEYCHAIN_PROFILE="$2"
      shift 2
      ;;
    --keychain)
      require_value "$1" "${2:-}"
      KEYCHAIN_PATH="$2"
      shift 2
      ;;
    --staple)
      require_value "$1" "${2:-}"
      STAPLE_TARGETS+=("$2")
      shift 2
      ;;
    --submission-id)
      require_value "$1" "${2:-}"
      SUBMISSION_ID="$2"
      shift 2
      ;;
    --submission-id-file)
      require_value "$1" "${2:-}"
      SUBMISSION_ID_FILE="$2"
      shift 2
      ;;
    --submit-only)
      SUBMIT_ONLY=1
      shift
      ;;
    --timeout)
      require_value "$1" "${2:-}"
      WAIT_TIMEOUT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$FILE_PATH" || -z "$KEYCHAIN_PROFILE" ]]; then
  usage >&2
  exit 1
fi

if [[ -n "$SUBMISSION_ID" && "$SUBMIT_ONLY" -eq 1 ]]; then
  echo "--submission-id cannot be combined with --submit-only" >&2
  exit 1
fi

if [[ ! -e "$FILE_PATH" ]]; then
  echo "File not found: $FILE_PATH" >&2
  exit 1
fi

if [[ -z "$SUBMISSION_ID" ]]; then
  SUBMIT_ARGS=(
    xcrun
    notarytool
    submit
    "$FILE_PATH"
    --keychain-profile
    "$KEYCHAIN_PROFILE"
    --no-wait
    --output-format
    json
  )

  if [[ -n "$KEYCHAIN_PATH" ]]; then
    SUBMIT_ARGS+=(--keychain "$KEYCHAIN_PATH")
  fi

  submit_output="$("${SUBMIT_ARGS[@]}")"
  echo "$submit_output"

  submission_id="$(SUBMIT_OUTPUT="$submit_output" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["SUBMIT_OUTPUT"])
submission_id = payload.get("id")
if not submission_id:
    raise SystemExit(f"Missing submission id from notarytool submit output: {payload!r}")
print(submission_id)
PY
)"
else
  submission_id="$SUBMISSION_ID"
fi

if [[ -n "$SUBMISSION_ID_FILE" ]]; then
  mkdir -p "$(dirname "$SUBMISSION_ID_FILE")"
  printf '%s\n' "$submission_id" > "$SUBMISSION_ID_FILE"
fi

echo "Notary submission ID: ${submission_id}"

if [[ "$SUBMIT_ONLY" -eq 1 ]]; then
  echo "Submitted without waiting for notarization."
  exit 0
fi

echo "Waiting up to ${WAIT_TIMEOUT} for Apple notarization processing."

INFO_ARGS=(
  xcrun
  notarytool
  info
  "$submission_id"
  --keychain-profile
  "$KEYCHAIN_PROFILE"
  --output-format
  json
)

LOG_ARGS=(
  xcrun
  notarytool
  log
  "$submission_id"
  -
  --keychain-profile
  "$KEYCHAIN_PROFILE"
)

if [[ -n "$KEYCHAIN_PATH" ]]; then
  INFO_ARGS+=(--keychain "$KEYCHAIN_PATH")
  LOG_ARGS+=(--keychain "$KEYCHAIN_PATH")
fi

wait_timeout_seconds="$(WAIT_TIMEOUT="$WAIT_TIMEOUT" python3 - <<'PY'
import os
import re

raw = os.environ["WAIT_TIMEOUT"].strip().lower()
match = re.fullmatch(r"(\d+)([smhd]?)", raw)
if not match:
    raise SystemExit(f"Unsupported timeout format: {raw!r}")

value = int(match.group(1))
unit = match.group(2) or "s"
multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
print(value * multipliers[unit])
PY
)"

deadline_epoch="$(( $(date +%s) + wait_timeout_seconds ))"
last_status=""
last_summary=""

while true; do
  now_epoch="$(date +%s)"
  if (( now_epoch >= deadline_epoch )); then
    echo "Notarization timed out for submission ${submission_id} after ${WAIT_TIMEOUT}." >&2
    "${INFO_ARGS[@]}" >&2 || true
    "${LOG_ARGS[@]}" >&2 || true
    exit 1
  fi

  if info_output="$("${INFO_ARGS[@]}" 2>&1)"; then
    status_and_summary="$(INFO_OUTPUT="$info_output" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["INFO_OUTPUT"])
status = (payload.get("status") or "").strip()
if not status:
    raise SystemExit(f"Missing status from notarytool info output: {payload!r}")

summary = (payload.get("statusSummary") or payload.get("message") or "").replace("\n", " ").strip()
print(status)
print(summary)
PY
)"
    status="$(printf '%s\n' "$status_and_summary" | sed -n '1p')"
    summary="$(printf '%s\n' "$status_and_summary" | sed -n '2p')"

    if [[ "$status" != "$last_status" || "$summary" != "$last_summary" ]]; then
      if [[ -n "$summary" ]]; then
        echo "Notary status: ${status} - ${summary}"
      else
        echo "Notary status: ${status}"
      fi
      last_status="$status"
      last_summary="$summary"
    fi

    case "$status" in
      Accepted)
        break
        ;;
      "In Progress")
        :
        ;;
      *)
        echo "Notarization failed for submission ${submission_id} with status ${status}." >&2
        echo "$info_output" >&2
        "${LOG_ARGS[@]}" >&2 || true
        exit 1
        ;;
    esac
  else
    remaining_seconds="$(( deadline_epoch - now_epoch ))"
    echo "Notary status check failed for submission ${submission_id}; retrying in ${STATUS_POLL_INTERVAL_SECONDS}s (${remaining_seconds}s remaining)." >&2
    echo "$info_output" >&2
  fi

  sleep "$STATUS_POLL_INTERVAL_SECONDS"
done

for staple_target in "${STAPLE_TARGETS[@]}"; do
  xcrun stapler staple -v "$staple_target"
  xcrun stapler validate -v "$staple_target"
done

echo "Notarized file: ${FILE_PATH}"
