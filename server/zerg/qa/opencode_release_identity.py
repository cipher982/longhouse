"""Strict identity qualification for an exact OpenCode executable."""

from __future__ import annotations

from pathlib import Path

from zerg.qa import provider_release_identity as identity

PROFILE = "opencode_release_identity_v1"
SCENARIO_ID = "opencode_release_identity"
ASSERTIONS = identity.ASSERTIONS
VERSION_LINE = identity.semver_version_line()
_PROFILE = identity.IdentityProfile(
    provider="opencode",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=VERSION_LINE,
    oracle_source=Path(__file__),
)
run = identity.identity_runner(_PROFILE)
