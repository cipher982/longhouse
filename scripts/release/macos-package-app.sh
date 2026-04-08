#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-package-app.sh \
  --binary <path> \
  --app-name <name> \
  --exec-name <name> \
  --bundle-id <id> \
  --version <build-version> \
  --short-version <marketing-version> \
  --output-dir <dir> \
  [--min-macos <version>] \
  [--category <uti>] \
  [--lsuielement true|false]

Creates a minimal macOS .app bundle around an existing executable.
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

BINARY_PATH=""
APP_NAME=""
EXEC_NAME=""
BUNDLE_ID=""
VERSION=""
SHORT_VERSION=""
OUTPUT_DIR=""
MIN_MACOS="14.0"
CATEGORY="public.app-category.developer-tools"
LSUIELEMENT="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --binary)
      require_value "$1" "${2:-}"
      BINARY_PATH="$2"
      shift 2
      ;;
    --app-name)
      require_value "$1" "${2:-}"
      APP_NAME="$2"
      shift 2
      ;;
    --exec-name)
      require_value "$1" "${2:-}"
      EXEC_NAME="$2"
      shift 2
      ;;
    --bundle-id)
      require_value "$1" "${2:-}"
      BUNDLE_ID="$2"
      shift 2
      ;;
    --version)
      require_value "$1" "${2:-}"
      VERSION="$2"
      shift 2
      ;;
    --short-version)
      require_value "$1" "${2:-}"
      SHORT_VERSION="$2"
      shift 2
      ;;
    --output-dir)
      require_value "$1" "${2:-}"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --min-macos)
      require_value "$1" "${2:-}"
      MIN_MACOS="$2"
      shift 2
      ;;
    --category)
      require_value "$1" "${2:-}"
      CATEGORY="$2"
      shift 2
      ;;
    --lsuielement)
      require_value "$1" "${2:-}"
      LSUIELEMENT="$2"
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

for required in BINARY_PATH APP_NAME EXEC_NAME BUNDLE_ID VERSION SHORT_VERSION OUTPUT_DIR; do
  if [[ -z "${!required}" ]]; then
    echo "Missing required argument: ${required}" >&2
    usage >&2
    exit 1
  fi
done

if [[ ! -f "$BINARY_PATH" ]]; then
  echo "Binary not found: $BINARY_PATH" >&2
  exit 1
fi

LSUIELEMENT_NORMALIZED="$(printf '%s' "$LSUIELEMENT" | tr '[:upper:]' '[:lower:]')"

case "$LSUIELEMENT_NORMALIZED" in
  true|yes|1)
    LSUIELEMENT_XML="<true/>"
    ;;
  false|no|0)
    LSUIELEMENT_XML="<false/>"
    ;;
  *)
    echo "--lsuielement must be true or false" >&2
    exit 1
    ;;
esac

APP_DIR="${OUTPUT_DIR}/${APP_NAME}.app"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "${CONTENTS_DIR}/Resources"
cp "$BINARY_PATH" "${MACOS_DIR}/${EXEC_NAME}"
chmod +x "${MACOS_DIR}/${EXEC_NAME}"

cat > "${CONTENTS_DIR}/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>${APP_NAME}</string>
  <key>CFBundleExecutable</key>
  <string>${EXEC_NAME}</string>
  <key>CFBundleIdentifier</key>
  <string>${BUNDLE_ID}</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>${APP_NAME}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>${SHORT_VERSION}</string>
  <key>CFBundleVersion</key>
  <string>${VERSION}</string>
  <key>LSApplicationCategoryType</key>
  <string>${CATEGORY}</string>
  <key>LSMinimumSystemVersion</key>
  <string>${MIN_MACOS}</string>
  <key>LSUIElement</key>
  ${LSUIELEMENT_XML}
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
EOF

printf 'APPL????' > "${CONTENTS_DIR}/PkgInfo"
plutil -lint "${CONTENTS_DIR}/Info.plist" >/dev/null

echo "Created app bundle: ${APP_DIR}"
