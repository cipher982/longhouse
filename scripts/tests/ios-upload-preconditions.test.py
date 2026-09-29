#!/usr/bin/env python3
"""Static guard for what App Store Connect rejects after an upload.

The archive-time check (ios/scripts/check-archive.sh) proves the built bundles; this
one fails the pull request that removes a precondition, without needing Xcode.
"""

from __future__ import annotations

import plistlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios"


def main() -> None:
    manifest = plistlib.loads((IOS / "Resources" / "PrivacyInfo.xcprivacy").read_bytes())
    assert manifest["NSPrivacyTracking"] is False, "the app must declare no tracking"
    reasons = {
        entry["NSPrivacyAccessedAPIType"]: set(entry["NSPrivacyAccessedAPITypeReasons"]) for entry in manifest["NSPrivacyAccessedAPITypes"]
    }
    assert {"CA92.1", "1C8F.1"} <= reasons["NSPrivacyAccessedAPICategoryUserDefaults"], reasons
    assert "C617.1" in reasons["NSPrivacyAccessedAPICategoryFileTimestamp"], reasons

    project = (IOS / "XcodeHarness" / "project.yml").read_text()
    assert project.count("../Resources/PrivacyInfo.xcprivacy") == 2, "the privacy manifest must ship in both the app and the widget target"
    assert "ITSAppUsesNonExemptEncryption: false" in project, "export-compliance answer missing from project.yml"

    info = plistlib.loads((IOS / "XcodeHarness" / "Info.plist").read_bytes())
    assert info.get("ITSAppUsesNonExemptEncryption") is False, "regenerate the project: Info.plist is stale"

    beta = tomllib.loads((IOS / "testflight" / "beta.toml").read_text())
    for key in ("bundle_id", "locale", "group_name", "public_link_limit"):
        assert beta.get(key), f"beta.toml missing {key}"
    for key in ("description", "privacy_policy_url", "marketing_url"):
        assert beta["localization"].get(key), f"beta.toml localization missing {key}"
    assert "Explore the demo" in beta["review"]["notes"], "review notes must point Apple at the no-account path"

    print("ios-upload-preconditions.test: ok")


if __name__ == "__main__":
    main()
