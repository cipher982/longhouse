#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_PATH="$ROOT_DIR/desktop/LonghouseMenuBarHarness"
ARTIFACT_DIR="$ROOT_DIR/artifacts/runtime-packaging"
STAGE_DIR="$ARTIFACT_DIR/stage"
APP_PATH="$STAGE_DIR/Longhouse.app"
ARCHIVE_PATH="$ARTIFACT_DIR/longhouse-local-health-app-darwin-arm64.zip"
MANIFEST_PATH="$ARTIFACT_DIR/manifest.json"

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  fail "test-local-runtime-packaging.sh requires macOS"
fi

require_cmd swift
require_cmd codesign
require_cmd ditto

mkdir -p "$ARTIFACT_DIR"
rm -rf "$STAGE_DIR" "$ARCHIVE_PATH" "$MANIFEST_PATH"

log "🏗️  Building macOS menu bar binary..."
swift build --package-path "$PACKAGE_PATH" -c release --product LonghouseMenuBarHarnessMenuBar >/dev/null
BIN_DIR="$(swift build --package-path "$PACKAGE_PATH" -c release --show-bin-path)"

log "📦 Packaging Longhouse.app..."
"$ROOT_DIR/scripts/release/macos-package-app.sh" \
  --binary "$BIN_DIR/LonghouseMenuBarHarnessMenuBar" \
  --app-name Longhouse \
  --exec-name Longhouse \
  --bundle-id ai.longhouse.app \
  --version 0.0.0-smoke \
  --short-version 0.0.0-smoke \
  --output-dir "$STAGE_DIR" \
  --icon-png "$ROOT_DIR/web/public/favicon-512.png" \
  --lsuielement true >/dev/null

log "✍️  Ad-hoc signing Longhouse.app..."
"$ROOT_DIR/scripts/release/macos-sign-app.sh" \
  --app "$APP_PATH" \
  --identity - \
  --mode adhoc >/dev/null

log "🗜️  Creating release archive..."
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ARCHIVE_PATH"

python3 - <<'PY' "$APP_PATH/Contents/Info.plist" "$ARCHIVE_PATH" "$MANIFEST_PATH"
import json
import os
import plistlib
import sys
import zipfile

plist_path, archive_path, manifest_path = sys.argv[1:4]

with open(plist_path, "rb") as fh:
    plist = plistlib.load(fh)

assert plist["CFBundleName"] == "Longhouse"
assert plist["CFBundleExecutable"] == "Longhouse"
assert plist["CFBundleIdentifier"] == "ai.longhouse.app"
assert plist["LSUIElement"] is True

with zipfile.ZipFile(archive_path) as archive:
    names = set(archive.namelist())

required_names = {
    "Longhouse.app/Contents/Info.plist",
    "Longhouse.app/Contents/MacOS/Longhouse",
    "Longhouse.app/Contents/Resources/AppIcon.icns",
}
missing = sorted(required_names - names)
if missing:
    raise SystemExit(f"Archive missing expected paths: {missing}")

manifest = {
    "schema_version": 1,
    "app_name": "Longhouse",
    "bundle_id": "ai.longhouse.app",
    "archive": archive_path,
    "archive_size_bytes": os.path.getsize(archive_path),
    "info_plist": plist_path,
}
with open(manifest_path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
PY

log "✅ Canonical Longhouse.app packaging smoke passed"
log "   app: $APP_PATH"
log "   zip: $ARCHIVE_PATH"
log "   manifest: $MANIFEST_PATH"
