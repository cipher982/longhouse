#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-sign-app.sh --app <path> --identity <identity> [--mode adhoc|developer-id]

Signs the main executable inside a macOS .app bundle, then signs the bundle.
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
IDENTITY=""
MODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app)
      require_value "$1" "${2:-}"
      APP_PATH="$2"
      shift 2
      ;;
    --identity)
      require_value "$1" "${2:-}"
      IDENTITY="$2"
      shift 2
      ;;
    --mode)
      require_value "$1" "${2:-}"
      MODE="$2"
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

if [[ -z "$APP_PATH" || -z "$IDENTITY" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -d "$APP_PATH" ]]; then
  echo "App bundle not found: $APP_PATH" >&2
  exit 1
fi

if [[ -z "$MODE" ]]; then
  if [[ "$IDENTITY" == "-" ]]; then
    MODE="adhoc"
  else
    MODE="developer-id"
  fi
fi

case "$MODE" in
  adhoc)
    SIGN_ARGS=(--force --sign "$IDENTITY")
    ;;
  developer-id)
    SIGN_ARGS=(--force --options runtime --timestamp --sign "$IDENTITY")
    ;;
  *)
    echo "--mode must be adhoc or developer-id" >&2
    exit 1
    ;;
esac

find "${APP_PATH}/Contents/MacOS" -maxdepth 1 -type f -print0 | while IFS= read -r -d '' executable; do
  codesign "${SIGN_ARGS[@]}" "$executable"
done

codesign "${SIGN_ARGS[@]}" "$APP_PATH"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

if [[ "$MODE" == "developer-id" ]]; then
  spctl --assess --type execute -vv "$APP_PATH" || true
fi

echo "Signed app bundle (${MODE}): ${APP_PATH}"
