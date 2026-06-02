#!/usr/bin/env bash
# Render every #Preview in the iOS app to PNG via SnapshotPreviews + xcodebuild test.
# Usage: ios/scripts/render-previews.sh [output-dir]
#
# Default output: /tmp/lh-previews
# Override build cache with LH_DERIVED_DATA_PATH=/tmp/custom-derived-data.
# Each preview is attached to the .xcresult by SnapshotPreviews and extracted
# by xcresulttool. Filenames look like preview-<TypeName>-<index>.png.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IOS_DIR="$REPO_ROOT/ios"
OUT_DIR="${1:-/tmp/lh-previews}"
RESULT_BUNDLE="/tmp/lh-previews.xcresult"
DERIVED_DATA_PATH="${LH_DERIVED_DATA_PATH:-/tmp/lh-previews-derived-data}"
SIM_NAME="${LH_SIM_NAME:-iPhone 17 Pro}"

cd "$IOS_DIR"

# Boot the simulator if it isn't already (simctl boot is idempotent-ish).
SIM_ID="$(xcrun simctl list devices available | awk -v name="$SIM_NAME" '
  $0 ~ name && /\(.*\)/ { match($0, /\([A-F0-9-]{36}\)/); print substr($0, RSTART+1, RLENGTH-2); exit }
')"
if [ -z "$SIM_ID" ]; then
  echo "Could not find simulator named '$SIM_NAME'" >&2
  exit 1
fi
xcrun simctl boot "$SIM_ID" 2>/dev/null || true

# Wipe prior artifacts. Keep previews isolated from Xcode's shared DerivedData
# so regenerated harness projects do not reuse stale source membership.
rm -rf "$RESULT_BUNDLE" "$OUT_DIR" "$DERIVED_DATA_PATH"
mkdir -p "$OUT_DIR"

echo ">> Running snapshot tests on $SIM_NAME ($SIM_ID)"
# Use -quiet to keep output tractable. SnapshotPreviews discovers previews at
# runtime, so a "test" failure isn't necessarily a UI bug — could be a preview
# that crashes during render. We still want the attachments either way.
set +e
xcodebuild test \
  -project XcodeHarness/LonghouseIOS.xcodeproj \
  -scheme Longhouse \
  -destination "platform=iOS Simulator,id=$SIM_ID" \
  -configuration Debug \
  -only-testing:LonghouseIOSTests/PreviewSnapshots \
  -resultBundlePath "$RESULT_BUNDLE" \
  -derivedDataPath "$DERIVED_DATA_PATH" \
  -quiet
TEST_EXIT=$?
set -e

echo ">> Extracting attachments"
xcrun xcresulttool export attachments \
  --path "$RESULT_BUNDLE" \
  --output-path "$OUT_DIR" \
  >/dev/null 2>&1 || true

# xcresulttool drops files as <uuid>.png plus a manifest.json mapping uuid →
# human readable name. Rename in-place so the agent can find the right preview.
MANIFEST="$OUT_DIR/manifest.json"
if [ -f "$MANIFEST" ]; then
  python3 - "$OUT_DIR" <<'PY'
import json, os, re, shutil, sys
out = sys.argv[1]
manifest = json.load(open(os.path.join(out, "manifest.json")))
records = []
def walk(node):
    if isinstance(node, list):
        for n in node: walk(n)
    elif isinstance(node, dict):
        for att in node.get("attachments", []) or []:
            records.append((att.get("exportedFileName"), att.get("suggestedHumanReadableName") or att.get("filename")))
        for v in node.values(): walk(v)
walk(manifest)
# The SnapshotPreviews names look like
# "Longhouse_InboxViewPreviews_0_<uuid>.swift_Timeline_cards_all_states"
# Strip the noisy uuid+swift artifacts.
def cleanup(name):
    # drop the embedded uuid + .swift_
    name = re.sub(r"_[0-9A-F-]{36}\.swift_", "_", name)
    # drop leading "Longhouse_"
    name = re.sub(r"^Longhouse_", "", name)
    return name
seen = {}
for src, name in records:
    if not src or not name: continue
    src_path = os.path.join(out, src)
    if not os.path.exists(src_path): continue
    base = cleanup(name)
    # ensure png extension
    if not base.lower().endswith(".png"):
        base += ".png"
    safe = base.replace("/", "_").replace(" ", "_")
    if safe in seen:
        seen[safe] += 1
        root, ext = os.path.splitext(safe)
        safe = f"{root}-{seen[safe]}{ext}"
    else:
        seen[safe] = 1
    dst = os.path.join(out, safe)
    shutil.move(src_path, dst)
PY
fi

echo
echo ">> PNGs in $OUT_DIR:"
ls -1 "$OUT_DIR" | grep -i '\.png$' || echo "(none — check $RESULT_BUNDLE for failures)"
echo
exit $TEST_EXIT
