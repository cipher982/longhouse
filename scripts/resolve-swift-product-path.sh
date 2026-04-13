#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: resolve-swift-product-path.sh --package-path <path> --product <name> [--configuration <debug|release>] [--no-build]

Builds a SwiftPM product by default and prints the resolved executable path.
Falls back to searching under .build when `swift build --show-bin-path` points
to a directory that does not actually contain the built binary.
EOF
}

PACKAGE_PATH=""
PRODUCT_NAME=""
CONFIGURATION="release"
BUILD_PRODUCT=1

require_value() {
  local flag="$1"
  local value="${2:-}"
  if [[ -z "$value" ]]; then
    echo "Missing value for $flag" >&2
    usage >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --package-path)
      require_value "$1" "${2:-}"
      PACKAGE_PATH="$2"
      shift 2
      ;;
    --product)
      require_value "$1" "${2:-}"
      PRODUCT_NAME="$2"
      shift 2
      ;;
    --configuration)
      require_value "$1" "${2:-}"
      CONFIGURATION="$2"
      shift 2
      ;;
    --no-build)
      BUILD_PRODUCT=0
      shift
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

if [[ -z "$PACKAGE_PATH" || -z "$PRODUCT_NAME" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -d "$PACKAGE_PATH" ]]; then
  echo "Package path not found: $PACKAGE_PATH" >&2
  exit 1
fi

if [[ "$BUILD_PRODUCT" == "1" ]]; then
  swift build --package-path "$PACKAGE_PATH" -c "$CONFIGURATION" --product "$PRODUCT_NAME" >/dev/null
fi

BIN_DIR="$(swift build --package-path "$PACKAGE_PATH" -c "$CONFIGURATION" --show-bin-path)"
PRIMARY_CANDIDATE="$BIN_DIR/$PRODUCT_NAME"
if [[ -x "$PRIMARY_CANDIDATE" ]]; then
  printf '%s\n' "$PRIMARY_CANDIDATE"
  exit 0
fi

CONFIGURATION_CAPITALIZED="${CONFIGURATION^}"
FALLBACK_CANDIDATE="$(
  find "$PACKAGE_PATH/.build" -type f -perm -111 \
    \( -path "*/${CONFIGURATION}*/${PRODUCT_NAME}" -o -path "*/${CONFIGURATION_CAPITALIZED}*/${PRODUCT_NAME}" \) \
    2>/dev/null | sort | head -1
)"
if [[ -n "$FALLBACK_CANDIDATE" ]]; then
  printf '%s\n' "$FALLBACK_CANDIDATE"
  exit 0
fi

ANY_CANDIDATE="$(find "$PACKAGE_PATH/.build" -type f -name "$PRODUCT_NAME" -perm -111 2>/dev/null | sort | head -1)"
if [[ -n "$ANY_CANDIDATE" ]]; then
  printf '%s\n' "$ANY_CANDIDATE"
  exit 0
fi

echo "Binary not found: $PRIMARY_CANDIDATE" >&2
exit 1
