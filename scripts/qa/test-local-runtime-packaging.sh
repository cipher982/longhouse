#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_PATH="$ROOT_DIR/desktop/LonghouseMenuBarHarness"
ARTIFACT_DIR="$ROOT_DIR/artifacts/runtime-packaging"
STAGE_DIR="$ARTIFACT_DIR/stage"
APP_PATH="$STAGE_DIR/Longhouse.app"
ARCHIVE_PATH="$ARTIFACT_DIR/Longhouse-macos-arm64.zip"
DISK_IMAGE_PATH="$ARTIFACT_DIR/Longhouse-macos-arm64.dmg"
MANIFEST_PATH="$ARTIFACT_DIR/manifest.json"
MOUNT_POINT=""
FIXTURE_PATH="$ROOT_DIR/desktop/LonghouseMenuBarHarness/Fixtures/healthy.json"

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
require_cmd hdiutil

cleanup() {
  if [[ -n "$MOUNT_POINT" ]] && mount | grep -F "on $MOUNT_POINT " >/dev/null 2>&1; then
    hdiutil detach "$MOUNT_POINT" >/dev/null || true
  fi
}
trap cleanup EXIT

mkdir -p "$ARTIFACT_DIR"
rm -rf "$STAGE_DIR" "$ARCHIVE_PATH" "$DISK_IMAGE_PATH" "$MANIFEST_PATH"

log "🏗️  Building macOS menu bar binary..."
MENUBAR_BINARY="$("$ROOT_DIR/scripts/resolve-swift-product-path.sh" \
  --package-path "$PACKAGE_PATH" \
  --product LonghouseMenuBarHarnessMenuBar \
  --configuration release)"

log "📦 Packaging Longhouse.app..."
"$ROOT_DIR/scripts/release/macos-package-app.sh" \
  --binary "$MENUBAR_BINARY" \
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

log "💿 Creating public disk image..."
"$ROOT_DIR/scripts/release/macos-package-dmg.sh" \
  --app "$APP_PATH" \
  --output "$DISK_IMAGE_PATH" >/dev/null

log "🔏 Ad-hoc signing public disk image..."
"$ROOT_DIR/scripts/release/macos-sign-disk-image.sh" \
  --dmg "$DISK_IMAGE_PATH" \
  --identity - \
  --mode adhoc >/dev/null

MOUNT_INFO="$(hdiutil attach -nobrowse -readonly -plist "$DISK_IMAGE_PATH")"
MOUNT_POINT="$(MOUNT_INFO="$MOUNT_INFO" python3 - <<'PY'
import os
import plistlib

payload = plistlib.loads(os.environ["MOUNT_INFO"].encode())
entities = payload.get("system-entities") or []
for entity in entities:
    mount_point = entity.get("mount-point")
    if mount_point:
        print(mount_point)
        break
else:
    raise SystemExit("Unable to determine mounted DMG path")
PY
)"

python3 - <<'PY' "$APP_PATH/Contents/Info.plist" "$ARCHIVE_PATH" "$DISK_IMAGE_PATH" "$MOUNT_POINT" "$MANIFEST_PATH"
import json
import os
import plistlib
import sys
import zipfile

plist_path, archive_path, dmg_path, mount_point, manifest_path = sys.argv[1:6]

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
    "Longhouse.app/Contents/Resources/LonghouseMenuBarHarness_LonghouseMenuBarCore.bundle/LonghouseMenuIcon.png",
    "Longhouse.app/Contents/Resources/LonghouseMenuBarHarness_LonghouseMenuBarCore.bundle/desktop-app-setup.sh",
}
missing = sorted(required_names - names)
if missing:
    raise SystemExit(f"Archive missing expected paths: {missing}")

disk_image_entries = {
    "Longhouse.app",
    "Applications",
}
actual_entries = set(os.listdir(mount_point))
missing_disk_image_entries = sorted(disk_image_entries - actual_entries)
if missing_disk_image_entries:
    raise SystemExit(f"Disk image missing expected entries: {missing_disk_image_entries}")

manifest = {
    "schema_version": 3,
    "app_name": "Longhouse",
    "bundle_id": "ai.longhouse.app",
    "archive": archive_path,
    "archive_size_bytes": os.path.getsize(archive_path),
    "disk_image": dmg_path,
    "disk_image_size_bytes": os.path.getsize(dmg_path),
    "info_plist": plist_path,
}
with open(manifest_path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
PY

log "🚦 Launching packaged app smoke..."
python3 - <<'PY' "$APP_PATH" "$FIXTURE_PATH"
import subprocess
import sys
from pathlib import Path

app_path = Path(sys.argv[1])
fixture_path = Path(sys.argv[2])
executable = app_path / "Contents" / "MacOS" / "Longhouse"
if not executable.exists():
    raise SystemExit(f"Missing packaged executable: {executable}")

completed = subprocess.run(
    [
        str(executable),
        "--input",
        str(fixture_path),
        "--quit-after",
        "1",
    ],
    capture_output=True,
    text=True,
    timeout=10,
)
if completed.returncode != 0:
    raise SystemExit(
        "Packaged app launch smoke failed:\n"
        f"exit={completed.returncode}\n"
        f"stdout={completed.stdout}\n"
        f"stderr={completed.stderr}"
    )
PY

cleanup
MOUNT_POINT=""

log "✅ Canonical Longhouse.app packaging smoke passed"
log "   app: $APP_PATH"
log "   zip: $ARCHIVE_PATH"
log "   dmg: $DISK_IMAGE_PATH"
log "   manifest: $MANIFEST_PATH"
