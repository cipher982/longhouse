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
  [--icon-png <path>] \
  [--build-identity <path>] \
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
ICON_PNG=""
BUILD_IDENTITY=""
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
    --icon-png)
      require_value "$1" "${2:-}"
      ICON_PNG="$2"
      shift 2
      ;;
    --build-identity)
      require_value "$1" "${2:-}"
      BUILD_IDENTITY="$2"
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
RESOURCES_DIR="${CONTENTS_DIR}/Resources"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$BINARY_PATH" "${MACOS_DIR}/${EXEC_NAME}"
chmod +x "${MACOS_DIR}/${EXEC_NAME}"

BINARY_DIR="$(cd "$(dirname "$BINARY_PATH")" && pwd)"
while IFS= read -r -d '' RESOURCE_BUNDLE; do
  cp -R "$RESOURCE_BUNDLE" "${RESOURCES_DIR}/$(basename "$RESOURCE_BUNDLE")"
done < <(find "$BINARY_DIR" -maxdepth 1 -type d -name '*.bundle' -print0)

if [[ -n "$ICON_PNG" ]]; then
  if [[ ! -f "$ICON_PNG" ]]; then
    echo "Icon PNG not found: $ICON_PNG" >&2
    exit 1
  fi
  if ! command -v sips >/dev/null 2>&1; then
    echo "sips is required when --icon-png is provided" >&2
    exit 1
  fi
  if ! command -v iconutil >/dev/null 2>&1; then
    echo "iconutil is required when --icon-png is provided" >&2
    exit 1
  fi

  ICON_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-icon.XXXXXX")"
  ICONSET_DIR="${ICON_TMP_DIR}/AppIcon.iconset"
  PADDED_ICON="${ICON_TMP_DIR}/AppIcon-base.png"
  mkdir -p "$ICONSET_DIR"

  cleanup_icon_tmp() {
    rm -rf "$ICON_TMP_DIR"
  }
  trap cleanup_icon_tmp EXIT

  sips --padToHeightWidth 512 512 "$ICON_PNG" --out "$PADDED_ICON" >/dev/null

  make_icon() {
    local pixels="$1"
    local filename="$2"
    sips --resampleHeightWidth "$pixels" "$pixels" "$PADDED_ICON" --out "${ICONSET_DIR}/${filename}" >/dev/null
  }

  make_icon 16 icon_16x16.png
  make_icon 32 icon_16x16@2x.png
  make_icon 32 icon_32x32.png
  make_icon 64 icon_32x32@2x.png
  make_icon 128 icon_128x128.png
  make_icon 256 icon_128x128@2x.png
  make_icon 256 icon_256x256.png
  make_icon 512 icon_256x256@2x.png
  make_icon 512 icon_512x512.png
  make_icon 1024 icon_512x512@2x.png

  iconutil -c icns "$ICONSET_DIR" -o "${RESOURCES_DIR}/AppIcon.icns"
fi

if [[ -n "$BUILD_IDENTITY" ]]; then
  if [[ ! -f "$BUILD_IDENTITY" ]]; then
    echo "Build identity JSON not found: $BUILD_IDENTITY" >&2
    exit 1
  fi
  cp "$BUILD_IDENTITY" "${RESOURCES_DIR}/build-identity.json"
fi

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
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
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
  <key>LSMultipleInstancesProhibited</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
EOF

printf 'APPL????' > "${CONTENTS_DIR}/PkgInfo"
plutil -lint "${CONTENTS_DIR}/Info.plist" >/dev/null
if [[ -n "$ICON_PNG" ]]; then
  trap - EXIT
  cleanup_icon_tmp
fi

echo "Created app bundle: ${APP_DIR}"
