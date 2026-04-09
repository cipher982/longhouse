#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-notarize-app.sh \
  --app <path> \
  --archive <path> \
  --keychain-profile <profile> \
  [--keychain <path>] \
  [--timeout <duration>]

Creates a zip archive for a signed .app bundle, submits it to Apple's notary
service, staples the resulting ticket to the app bundle, then recreates the zip.
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

APP_PATH=""
ARCHIVE_PATH=""
KEYCHAIN_PROFILE=""
KEYCHAIN_PATH=""
WAIT_TIMEOUT="${LONGHOUSE_NOTARY_WAIT_TIMEOUT:-90m}"
STATUS_POLL_INTERVAL_SECONDS="${LONGHOUSE_NOTARY_POLL_INTERVAL_SECONDS:-60}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app)
      require_value "$1" "${2:-}"
      APP_PATH="$2"
      shift 2
      ;;
    --archive)
      require_value "$1" "${2:-}"
      ARCHIVE_PATH="$2"
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

if [[ -z "$APP_PATH" || -z "$ARCHIVE_PATH" || -z "$KEYCHAIN_PROFILE" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -d "$APP_PATH" ]]; then
  echo "App bundle not found: $APP_PATH" >&2
  exit 1
fi

mkdir -p "$(dirname "$ARCHIVE_PATH")"
rm -f "$ARCHIVE_PATH"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ARCHIVE_PATH"

SUBMIT_ARGS=(
  xcrun
  notarytool
  submit
  "$ARCHIVE_PATH"
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
import sys

try:
    payload = json.loads(os.environ["SUBMIT_OUTPUT"])
except json.JSONDecodeError as exc:
    raise SystemExit(f"Unable to parse notarytool submit output as JSON: {exc}") from exc

submission_id = payload.get("id")
if not submission_id:
    raise SystemExit(f"Missing submission id from notarytool submit output: {payload!r}")
print(submission_id)
PY
)"

echo "Notary submission ID: ${submission_id}"
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

value = os.environ["WAIT_TIMEOUT"].strip().lower()
match = re.fullmatch(r"(\d+)([smhd])", value)
if not match:
    raise SystemExit(f"Unsupported timeout format: {value!r}; expected e.g. 90m, 45s, 2h")

amount = int(match.group(1))
unit = match.group(2)
multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
print(amount * multiplier)
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
import sys

try:
    payload = json.loads(os.environ["INFO_OUTPUT"])
except json.JSONDecodeError as exc:
    raise SystemExit(f"Unable to parse notarytool info output as JSON: {exc}") from exc

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

xcrun stapler staple -v "$APP_PATH"
xcrun stapler validate -v "$APP_PATH"

rm -f "$ARCHIVE_PATH"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ARCHIVE_PATH"

echo "Notarized archive: ${ARCHIVE_PATH}"
