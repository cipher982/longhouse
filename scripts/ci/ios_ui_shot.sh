#!/usr/bin/env bash
# Run one iOS UI test under the smoke scheme (the only scheme that carries the
# UI test target) and export every screenshot it attached, so an agent can
# look at the rendered frame instead of trusting an assertion.
#
# Usage: scripts/ci/ios_ui_shot.sh SessionChatUITests/testTurnFooterRendersUnderTheProviderReply
# Live-session proof: export LONGHOUSE_FIDELITY_{SERVER_URL,AUTH_TOKEN,SESSION_ID,MARKERS_JSON}
# first, then select LiveSessionFidelityUITests/testRealSessionColdOpenAndReopen.
# Credentials travel only through process environment, never xctestrun/plist files.
# Output: artifacts/ios-ui-shot/<timestamp>/<attachment name>.png plus the
#         .xcresult bundle. Failure screenshots XCTest takes on its own are
#         exported too, so a failing run still leaves a frame to look at.
set -euo pipefail
if ! python3 "$(dirname "${BASH_SOURCE[0]}")/../qa/test_boundary.py"; then
  echo "Use make ios-ui-shot TEST=... to run in a disposable hosted macOS VM." >&2
  exit 2
fi
TEST="${1:?test id required, e.g. SessionChatUITests/testName}"
if [[ "$TEST" == LiveSessionFidelityUITests* ]]; then
  for suffix in SERVER_URL AUTH_TOKEN SESSION_ID MARKERS_JSON; do
    key="LONGHOUSE_FIDELITY_${suffix}"
    if [[ -z "${!key:-}" ]]; then
      echo "Live-session fidelity requires ${key}" >&2
      exit 2
    fi
  done
fi
PROJECT="ios/XcodeHarness/LonghouseIOS.xcodeproj"
SCHEME="LonghouseSmoke"
DERIVED_DATA_PATH="${IOS_DERIVED_DATA_PATH:-${HOME}/Library/Developer/Xcode/DerivedData/LonghouseIOS-CI}"
DESTINATION="${IOS_DESTINATION:-$(python3 scripts/ci/select_ios_simulator.py "$PROJECT" "$SCHEME")}"
if [[ ",${DESTINATION}," != *",platform=iOS Simulator,"* ]]; then
  echo "ios_ui_shot.sh only supports iOS Simulator destinations; no physical device will be touched" >&2
  exit 2
fi
OUT="artifacts/ios-ui-shot/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
chmod 700 "$OUT"
echo "Evidence directory: $OUT"

set +e
xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
  -derivedDataPath "$DERIVED_DATA_PATH" build-for-testing 2>&1 | tee "$OUT/build.log" | grep -E '\*\* BUILD|error:'
build_status=${PIPESTATUS[0]}
set -e
if [[ "$build_status" -ne 0 ]]; then
  echo "Build failed (exit $build_status); refusing to run a stale UI test bundle" >&2
  exit "$build_status"
fi
# xcodebuild strips TEST_RUNNER_ and injects the remainder into the UI test
# runner. Ordinary parent environment alone is not a test-host contract.
for suffix in SERVER_URL AUTH_TOKEN SESSION_ID MARKERS_JSON; do
  key="LONGHOUSE_FIDELITY_${suffix}"
  if [[ -n "${!key:-}" ]]; then
    export "TEST_RUNNER_${key}=${!key}"
  fi
done
set +e
# Keep the xcresult, test log and screenshots, but do not launch a lengthy
# sysdiagnose when a screenshot proof or deliberate negative control fails.
xcodebuild -project "$PROJECT" -scheme "$SCHEME" -destination "$DESTINATION" \
  -derivedDataPath "$DERIVED_DATA_PATH" -resultBundlePath "$OUT/result.xcresult" \
  -collect-test-diagnostics never \
  -only-testing:"LonghouseIOSUITests/$TEST" test-without-building 2>&1 | tee "$OUT/test.log" | grep -E 'Test Case|\*\* TEST|error:'
status=${PIPESTATUS[0]}
set -e
xcrun xcresulttool export attachments --path "$OUT/result.xcresult" --output-path "$OUT/attachments" >/dev/null 2>&1 || true
if [[ "$TEST" == LiveSessionFidelityUITests* && ! -f "$OUT/attachments/manifest.json" ]]; then
  echo "Live-session proof produced no exported evidence: $OUT/result.xcresult" >&2
  exit 1
fi
# xcresulttool names files by uuid; expose PNGs and JSON metrics by attachment name.
if [[ -f "$OUT/attachments/manifest.json" ]]; then
  python3 - "$OUT" "$TEST" <<'PY'
import json
import os
import sys

out = sys.argv[1]
manifest = json.load(open(os.path.join(out, "attachments", "manifest.json")))
fidelity_metrics = {}
fidelity_screenshots = set()
for test in manifest:
    for att in test.get("attachments", []):
        src = os.path.join(out, "attachments", att["exportedFileName"])
        name = att.get("suggestedHumanReadableName") or att["exportedFileName"]
        if src.endswith((".png", ".json")) and os.path.exists(src):
            dst = os.path.join(out, name.replace("/", "_"))
            os.replace(src, dst)
            print(dst)
            if name.startswith("fidelity-") and src.endswith(".json"):
                with open(dst) as handle:
                    receipt = json.load(handle)
                fidelity_metrics[receipt.get("phase")] = receipt
            elif name.startswith("fidelity-") and src.endswith(".png"):
                fidelity_screenshots.add(name)
if sys.argv[2].startswith("LiveSessionFidelityUITests"):
    # A skipped test, broken env forwarding, or missing attachment cannot be a
    # green proof, even when xcodebuild itself reports a successful invocation.
    expected = json.loads(os.environ["LONGHOUSE_FIDELITY_MARKERS_JSON"])
    for phase in ("cold-open", "terminate-reopen"):
        receipt = fidelity_metrics.get(phase, {})
        if (
            receipt.get("status") != "pass"
            or receipt.get("sessionID") != os.environ["LONGHOUSE_FIDELITY_SESSION_ID"]
            or receipt.get("markers") != expected
            or not any(name.startswith(f"fidelity-{phase}-pass") for name in fidelity_screenshots)
        ):
            raise SystemExit(f"Missing successful {phase} screenshot/metrics: {out}/result.xcresult")
PY
fi
echo "Result bundle: $OUT/result.xcresult (test exit $status)"
exit "$status"
