#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-sign-disk-image.sh --dmg <path> --identity <identity> [--mode adhoc|developer-id]

Signs a macOS disk image intended for direct download distribution.
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

DMG_PATH=""
IDENTITY=""
MODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dmg)
      require_value "$1" "${2:-}"
      DMG_PATH="$2"
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

if [[ -z "$DMG_PATH" || -z "$IDENTITY" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -f "$DMG_PATH" ]]; then
  echo "Disk image not found: $DMG_PATH" >&2
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
    SIGN_ARGS=(--force --timestamp --sign "$IDENTITY")
    ;;
  *)
    echo "--mode must be adhoc or developer-id" >&2
    exit 1
    ;;
esac

codesign "${SIGN_ARGS[@]}" "$DMG_PATH"
codesign --verify --verbose=2 "$DMG_PATH"

if [[ "$MODE" == "developer-id" ]]; then
  spctl --assess --type open -vv "$DMG_PATH" || true
fi

echo "Signed disk image (${MODE}): ${DMG_PATH}"
