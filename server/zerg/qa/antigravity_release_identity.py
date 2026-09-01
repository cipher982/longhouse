"""Strict identity qualification for an exact Antigravity executable."""

from __future__ import annotations

from pathlib import Path

from zerg.qa import provider_release_identity as identity

PROFILE = "antigravity_release_identity_v1"
SCENARIO_ID = "antigravity_release_identity"
ASSERTIONS = identity.ASSERTIONS
VERSION_LINE = identity.semver_version_line()
_PROFILE = identity.IdentityProfile(
    provider="antigravity",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=VERSION_LINE,
    oracle_source=Path(__file__),
)
run = identity.identity_runner(_PROFILE)
