#!/usr/bin/env bash
# Run one iOS UI test under the smoke scheme (the only scheme that carries the
# UI test target) and export every screenshot it attached, so an agent can
# look at the rendered frame instead of trusting an assertion.
#
# Usage: scripts/ci/ios_ui_shot.sh SessionChatUITests/testTurnFooterRendersUnderTheProviderReply
# Output: artifacts/ios-ui-shot/<timestamp>/<attachment name>.png plus the
#         .xcresult bundle. Failure screenshots XCTest takes on its own are
#         exported too, so a failing run still leaves a frame to look at.
set -euo pipefail
TEST="${1:?test id required, e.g. SessionChatUITests/testName}"
PROJECT="ios/XcodeHarness/LonghouseIOS.xcodeproj"
SCHEME="LonghouseSmoke"
DERIVED_DATA_PATH="${IOS_DERIVED_DATA_PATH:-${HOME}/Library/Developer/Xcode/DerivedData/LonghouseIOS-CI}"
DESTINATION="${IOS_DESTINATION:-$(python3 scripts/ci/select_ios_simulator.py "$PROJECT" "$SCHEME")}"
OUT="artifacts/ios-ui-shot/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"

xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
  -derivedDataPath "$DERIVED_DATA_PATH" build-for-testing 2>&1 | grep -E '\*\* BUILD|error:' || true
set +e
xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
  -derivedDataPath "$DERIVED_DATA_PATH" -resultBundlePath "$OUT/result.xcresult" \
  -only-testing:"LonghouseIOSUITests/$TEST" test-without-building 2>&1 | grep -E 'Test Case|\*\* TEST|error:'
status=${PIPESTATUS[0]}
set -e
xcrun xcresulttool export attachments --path "$OUT/result.xcresult" --output-path "$OUT/attachments" >/dev/null 2>&1 || true
# xcresulttool names files by uuid; rename PNGs to the attachment's own name.
if [[ -f "$OUT/attachments/manifest.json" ]]; then
  python3 - "$OUT" <<'PY'
import json
import os
import sys

out = sys.argv[1]
manifest = json.load(open(os.path.join(out, "attachments", "manifest.json")))
for test in manifest:
    for att in test.get("attachments", []):
        src = os.path.join(out, "attachments", att["exportedFileName"])
        name = att.get("suggestedHumanReadableName") or att["exportedFileName"]
        if src.endswith(".png") and os.path.exists(src):
            dst = os.path.join(out, name.replace("/", "_"))
            os.replace(src, dst)
            print(dst)
PY
fi
echo "Result bundle: $OUT/result.xcresult (test exit $status)"
exit "$status"
