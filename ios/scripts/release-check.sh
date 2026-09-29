#!/usr/bin/env bash
# Unsigned Release archive plus the App Store upload preconditions
# (check-archive.sh). Needs Xcode and no credentials, so it runs on the bench or a
# hosted runner.
#
#   ios/scripts/release-check.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

out="$(mktemp -d "${TMPDIR:-/tmp}/lh-release-check.XXXXXX")"
trap 'rm -rf -- "$out"' EXIT

make ios-project >/dev/null
xcodebuild archive \
  -project ios/XcodeHarness/LonghouseIOS.xcodeproj \
  -scheme Longhouse \
  -configuration Release \
  -destination "generic/platform=iOS" \
  -archivePath "$out/Longhouse.xcarchive" \
  CODE_SIGNING_ALLOWED=NO >"$out/archive.log" 2>&1 \
  || { tail -40 "$out/archive.log" >&2; echo "release-check: Release archive failed" >&2; exit 1; }

ios/scripts/check-archive.sh "$out/Longhouse.xcarchive"
