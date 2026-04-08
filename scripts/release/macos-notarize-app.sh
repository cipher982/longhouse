#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-notarize-app.sh \
  --app <path> \
  --archive <path> \
  --keychain-profile <profile> \
  [--keychain <path>]

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

NOTARY_ARGS=(
  xcrun
  notarytool
  submit
  "$ARCHIVE_PATH"
  --keychain-profile
  "$KEYCHAIN_PROFILE"
  --wait
)

if [[ -n "$KEYCHAIN_PATH" ]]; then
  NOTARY_ARGS+=(--keychain "$KEYCHAIN_PATH")
fi

"${NOTARY_ARGS[@]}"
xcrun stapler staple -v "$APP_PATH"
xcrun stapler validate -v "$APP_PATH"

rm -f "$ARCHIVE_PATH"
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ARCHIVE_PATH"

echo "Notarized archive: ${ARCHIVE_PATH}"
