#!/usr/bin/env bash
# Archive, sign and (optionally) upload the iOS app to App Store Connect.
#
#   ios/scripts/testflight-build.sh                # archive + export the .ipa only
#   ios/scripts/testflight-build.sh --upload       # ... and upload it to TestFlight
#
# The archive is built unsigned, then ad-hoc signed with each bundle's entitlements, and
# only the export signs for distribution, with Apple cloud-managed signing: with an App
# Store Connect API key (CI) Xcode uses the cloud distribution certificate and creates the
# profiles itself; without one it uses the Apple ID signed in to Xcode on this Mac (local
# dry runs). Nothing here installs a certificate or a profile by hand.
#
# Why the archive is not signed by Xcode: automatic signing signs an archive for
# development, so on a machine whose keychain holds no development identity (every hosted
# runner) it creates a new "Apple Development: Created via API" certificate per run, which
# dies with the VM and counts toward Apple's cap; revoking it emails the account holder.
# Why the ad-hoc signature: export takes each bundle's entitlement request from the
# archived signature, so an unsigned archive exports without aps-environment and the app
# group (push and the widget's shared data break silently). Both were observed 2026-10-07.
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

# The workflow ships an older SHA than the ref it was dispatched from, and CI's
# GITHUB_SHA names the dispatch ref. The build identity must describe the tree being
# archived (build number and the freshness guard both read HEAD), so ignore it.
unset GITHUB_SHA

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
# Safety net: this build should create no development certificate (see the header). If an
# Xcode change ever makes it create one, revoke what this run created, new since the
# snapshot and held by this keychain, so a concurrent build elsewhere keeps its own and
# leaked certificates never accumulate to Apple's cap. A non-zero `revoked_dev_certs` in
# the log means the unsigned archive path regressed.
certs_before=""
revoke_own_dev_certs() {
  security find-identity -p codesigning | awk '$1 ~ /^[0-9]+\)$/ { print $2 }' > "$work/held-identities" \
    && scripts/ops/testflight.py revoke-dev-certs --keep "$certs_before" --held "$work/held-identities" >&2
}
cleanup() {
  local status=$?
  if [ -n "$certs_before" ] && ! revoke_own_dev_certs; then
    echo "testflight-build: could not revoke this run's development certificate; each leaked one counts toward Apple's cap" >&2
    [ "$status" -ne 0 ] || status=1
  fi
  rm -rf -- "$work"
  exit "$status"
}
trap cleanup EXIT

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

if [ ${#auth_args[@]} -gt 0 ]; then
  scripts/ops/testflight.py dev-certs > "$work/dev-certs.before"
  certs_before="$work/dev-certs.before"
fi

archive="$out/Longhouse.xcarchive"
# No -allowProvisioningUpdates and no API key here: nothing in the archive step may talk to
# Apple's signing service.
xcodebuild archive \
  -project ios/XcodeHarness/LonghouseIOS.xcodeproj \
  -scheme Longhouse \
  -configuration Release \
  -destination "generic/platform=iOS" \
  -archivePath "$archive" \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGN_IDENTITY= \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER" >"$out/archive.log" 2>&1 \
  || { tail -40 "$out/archive.log" >&2; fail "archive failed (full log: $out/archive.log)"; }

ios/scripts/check-archive.sh "$archive" >&2

# Each signed bundle and its entitlements file, innermost first (codesign seals nested code).
app="$archive/Products/Applications/Longhouse.app"
bundles=("$app/PlugIns/LonghouseWidget.appex" "$app")
entitlement_files=(ios/XcodeHarness/LonghouseWidget.entitlements ios/XcodeHarness/Longhouse.entitlements)
# A new app extension or nested app carries its own entitlements and must be listed above;
# frameworks need none, and the export signs them.
while IFS= read -r nested; do
  case " ${bundles[*]} " in *" $nested "*) ;; *) fail "unlisted bundle ${nested#"$app/"}: add it and its entitlements to this script";; esac
done < <(find "$app" -mindepth 1 \( -name '*.appex' -o -name '*.app' \))
for i in "${!bundles[@]}"; do
  [ -d "${bundles[$i]}" ] || fail "listed bundle ${bundles[$i]#"$app/"} is not in the archive"
  # App Store builds always use production push; the files leave it to a build setting.
  sed 's/\$(APS_ENVIRONMENT)/production/' "${entitlement_files[$i]}" > "$work/entitlements.$i.plist"
  ! grep -q '\$(' "$work/entitlements.$i.plist" || fail "${entitlement_files[$i]} has a build setting this script does not expand"
  codesign --force --sign - --entitlements "$work/entitlements.$i.plist" "${bundles[$i]}" \
    || fail "could not ad-hoc sign ${bundles[$i]}"
done

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

# Xcode's packaging step calls `rsync` from PATH and fails ("Copy failed") with Homebrew's
# rsync 3.x, which the bench and dev Macs put first; the system one works.
PATH="/usr/bin:$PATH" xcodebuild -exportArchive \
  -archivePath "$archive" \
  -exportPath "$out/export" \
  -exportOptionsPlist "$work/ExportOptions.plist" \
  -allowProvisioningUpdates \
  ${auth_args[@]+"${auth_args[@]}"} >"$out/export.log" 2>&1 \
  || { tail -40 "$out/export.log" >&2; fail "export failed (full log: $out/export.log)"; }

ipa="$(find "$out/export" -maxdepth 1 -name '*.ipa' | head -1)"
[ -n "$ipa" ] || fail "export produced no .ipa"

# The exported signatures must carry every entitlement the project requests: a dropped
# one (the unsigned-archive failure) uploads fine and breaks push or the widget on device.
mkdir "$work/ipa"
ditto -x -k "$ipa" "$work/ipa"
for i in "${!bundles[@]}"; do
  exported="$work/ipa/Payload/${bundles[$i]#"$archive/Products/Applications/"}"
  codesign -d --entitlements - --xml "$exported" > "$work/exported.$i.plist" 2>/dev/null \
    || fail "could not read the exported signature of $exported"
  python3 -I -c '
import plistlib, sys
want = plistlib.load(open(sys.argv[1], "rb"))
got = plistlib.load(open(sys.argv[2], "rb"))
bad = sorted(k for k, v in want.items() if got.get(k) != v)
sys.exit(f"exported {sys.argv[3]} lacks or changed entitlements: {bad}" if bad else 0)
' "$work/entitlements.$i.plist" "$work/exported.$i.plist" "$(basename "$exported")" || fail "export dropped entitlements"
done

if [ "$upload" = 1 ]; then
  xcrun altool --upload-app --type ios --file "$ipa" \
    --apiKey "$ASC_API_KEY_ID" --apiIssuer "$ASC_API_ISSUER_ID" >"$out/upload.log" 2>&1 \
    || { tail -40 "$out/upload.log" >&2; fail "upload failed (full log: $out/upload.log)"; }
  echo "testflight-build: uploaded $(basename "$ipa")" >&2
fi

[ -z "${OUT_DIR:-}" ] || echo "ipa=$ipa"
echo "build_number=$BUILD_NUMBER"
echo "marketing_version=$MARKETING_VERSION"
