#!/usr/bin/env bash
# Archive, sign and (optionally) upload the iOS app to App Store Connect.
#
#   ios/scripts/testflight-build.sh                # archive + export the .ipa only
#   ios/scripts/testflight-build.sh --upload       # ... and upload it to TestFlight
#
# Signing is Apple cloud-managed: with an App Store Connect API key (CI) Xcode
# creates the distribution certificate and profiles itself; without one it uses
# the Apple ID signed in to Xcode on this Mac (local dry runs). Nothing here
# installs a certificate or a profile by hand.
#
# Environment:
#   ASC_API_KEY_ID, ASC_API_ISSUER_ID   API key identity (both or neither)
#   ASC_API_KEY_P8_B64                  base64 of AuthKey_<id>.p8 (or ASC_API_KEY_PATH)
#   BUILD_NUMBER                        CFBundleVersion; default: commits on HEAD
#   APPLE_TEAM_ID                       default M49WM6JSW8 (not a secret: every signed app carries it)
#   OUT_DIR                             keep artifacts here instead of a temp dir
#
# Prints `build_number=<n>` and `marketing_version=<v>` on success so the caller
# can wait for that exact build to finish processing.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

upload=0
[ "${1:-}" = "--upload" ] && upload=1

TEAM_ID="${APPLE_TEAM_ID:-M49WM6JSW8}"
BUILD_NUMBER="${BUILD_NUMBER:-$(git rev-list --count HEAD)}"
MARKETING_VERSION="$(sed -n 's/^MARKETING_VERSION = //p' ios/XcodeHarness/Configs/Version.xcconfig)"

fail() { echo "testflight-build: $*" >&2; exit 1; }
[ -n "$MARKETING_VERSION" ] || fail "no MARKETING_VERSION in Version.xcconfig"
case "$BUILD_NUMBER" in ''|*[!0-9]*) fail "BUILD_NUMBER must be an integer (got '$BUILD_NUMBER')";; esac

work="$(mktemp -d "${TMPDIR:-/tmp}/lh-testflight.XXXXXX")"
out="${OUT_DIR:-$work/out}"
mkdir -p "$out"
trap 'rm -rf -- "$work"' EXIT

# --- credentials: a key file for Xcode and altool, only when a key is configured
auth_args=()
if [ -n "${ASC_API_KEY_ID:-}" ]; then
  [ -n "${ASC_API_ISSUER_ID:-}" ] || fail "ASC_API_KEY_ID set without ASC_API_ISSUER_ID"
  keydir="$work/keys"; mkdir -m 700 "$keydir"
  keyfile="$keydir/AuthKey_${ASC_API_KEY_ID}.p8"
  if [ -n "${ASC_API_KEY_PATH:-}" ]; then
    cp "$ASC_API_KEY_PATH" "$keyfile"
  elif [ -n "${ASC_API_KEY_P8_B64:-}" ]; then
    printf '%s' "$ASC_API_KEY_P8_B64" | base64 --decode > "$keyfile"
  else
    fail "ASC_API_KEY_ID set without ASC_API_KEY_P8_B64 or ASC_API_KEY_PATH"
  fi
  chmod 600 "$keyfile"
  auth_args=(-authenticationKeyPath "$keyfile" -authenticationKeyID "$ASC_API_KEY_ID" -authenticationKeyIssuerID "$ASC_API_ISSUER_ID")
  export API_PRIVATE_KEYS_DIR="$keydir"
elif [ "$upload" = 1 ]; then
  fail "--upload needs ASC_API_KEY_ID / ASC_API_ISSUER_ID / ASC_API_KEY_P8_B64"
fi

echo "testflight-build: version $MARKETING_VERSION build $BUILD_NUMBER team $TEAM_ID" >&2

make ios-project >/dev/null

archive="$out/Longhouse.xcarchive"
xcodebuild archive \
  -project ios/XcodeHarness/LonghouseIOS.xcodeproj \
  -scheme Longhouse \
  -configuration Release \
  -destination "generic/platform=iOS" \
  -archivePath "$archive" \
  -allowProvisioningUpdates \
  ${auth_args[@]+"${auth_args[@]}"} \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER" >"$out/archive.log" 2>&1 \
  || { tail -40 "$out/archive.log" >&2; fail "archive failed (full log: $out/archive.log)"; }

ios/scripts/check-archive.sh "$archive" >&2

cat > "$work/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key><string>app-store-connect</string>
	<key>destination</key><string>export</string>
	<key>teamID</key><string>$TEAM_ID</string>
	<key>signingStyle</key><string>automatic</string>
	<key>uploadSymbols</key><true/>
	<key>manageAppVersionAndBuildNumber</key><false/>
</dict>
</plist>
PLIST

xcodebuild -exportArchive \
  -archivePath "$archive" \
  -exportPath "$out/export" \
  -exportOptionsPlist "$work/ExportOptions.plist" \
  -allowProvisioningUpdates \
  ${auth_args[@]+"${auth_args[@]}"} >"$out/export.log" 2>&1 \
  || { tail -40 "$out/export.log" >&2; fail "export failed (full log: $out/export.log)"; }

ipa="$(find "$out/export" -maxdepth 1 -name '*.ipa' | head -1)"
[ -n "$ipa" ] || fail "export produced no .ipa"

if [ "$upload" = 1 ]; then
  xcrun altool --upload-app --type ios --file "$ipa" \
    --apiKey "$ASC_API_KEY_ID" --apiIssuer "$ASC_API_ISSUER_ID" >"$out/upload.log" 2>&1 \
    || { tail -40 "$out/upload.log" >&2; fail "upload failed (full log: $out/upload.log)"; }
  echo "testflight-build: uploaded $(basename "$ipa")" >&2
fi

[ -z "${OUT_DIR:-}" ] || echo "ipa=$ipa"
echo "build_number=$BUILD_NUMBER"
echo "marketing_version=$MARKETING_VERSION"
