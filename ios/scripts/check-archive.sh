#!/usr/bin/env bash
# Assert what App Store Connect would otherwise only reject after an upload:
# a privacy manifest in the app and the widget, and the export-compliance answer.
#
#   ios/scripts/check-archive.sh <path/to/Longhouse.xcarchive>
set -euo pipefail

archive="${1:?usage: check-archive.sh <Longhouse.xcarchive>}"
app="$archive/Products/Applications/Longhouse.app"
widget="$app/PlugIns/LonghouseWidget.appex"

fail() { echo "check-archive: $*" >&2; exit 1; }

[ -d "$app" ] || fail "no Longhouse.app in $archive"
[ -f "$app/PrivacyInfo.xcprivacy" ] || fail "app bundle has no PrivacyInfo.xcprivacy"
[ -f "$widget/PrivacyInfo.xcprivacy" ] || fail "widget bundle has no PrivacyInfo.xcprivacy"
plutil -lint "$app/PrivacyInfo.xcprivacy" >/dev/null

widget_name="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleDisplayName' "$widget/Info.plist" 2>/dev/null || true)"
[ -n "$widget_name" ] || fail "widget Info.plist has no CFBundleDisplayName (App Store Connect rejects the upload)"

# App Store Connect only accepts builds made with the iOS 26 SDK (Xcode 26) or later.
sdk="$(/usr/libexec/PlistBuddy -c 'Print :DTSDKName' "$app/Info.plist" 2>/dev/null || true)"
sdk_major="$(printf '%s' "$sdk" | sed -n 's/^iphoneos\([0-9][0-9]*\).*/\1/p')"
[ -n "$sdk_major" ] && [ "$sdk_major" -ge 26 ] || fail "built with SDK '${sdk:-unknown}'; App Store Connect requires the iOS 26 SDK or later (Xcode 26+)"

encryption="$(/usr/libexec/PlistBuddy -c 'Print :ITSAppUsesNonExemptEncryption' "$app/Info.plist" 2>/dev/null || true)"
[ "$encryption" = "false" ] || fail "ITSAppUsesNonExemptEncryption must be false in the app Info.plist (got '${encryption:-missing}')"

echo "check-archive: ok ($(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Info.plist") build $(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$app/Info.plist"))"
