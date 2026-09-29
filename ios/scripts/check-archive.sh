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

encryption="$(/usr/libexec/PlistBuddy -c 'Print :ITSAppUsesNonExemptEncryption' "$app/Info.plist" 2>/dev/null || true)"
[ "$encryption" = "false" ] || fail "ITSAppUsesNonExemptEncryption must be false in the app Info.plist (got '${encryption:-missing}')"

echo "check-archive: ok ($(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Info.plist") build $(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$app/Info.plist"))"
