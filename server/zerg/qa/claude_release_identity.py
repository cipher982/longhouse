"""Strict identity qualification for an exact Claude Code executable."""

from __future__ import annotations

from pathlib import Path

from zerg.qa import provider_release_identity as identity

PROFILE = "claude_release_identity_v1"
SCENARIO_ID = "claude_release_identity"
ASSERTIONS = identity.ASSERTIONS
VERSION_LINE = identity.semver_version_line(r" \(Claude Code\)")
_PROFILE = identity.IdentityProfile(
    provider="claude",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=VERSION_LINE,
    oracle_source=Path(__file__),
)
run = identity.identity_runner(_PROFILE)
