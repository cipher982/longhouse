"""Strict identity qualification for an exact Claude Code executable."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from zerg.qa import provider_release_identity as identity

PROFILE = "claude_release_identity_v1"
SCENARIO_ID = "claude_release_identity"
ASSERTIONS = identity.ASSERTIONS
VERSION_LINE = re.compile(rf"^(?P<version>{identity.SEMVER}) \(Claude Code\)$")
_PROFILE = identity.IdentityProfile(
    provider="claude",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=VERSION_LINE,
    oracle_source=Path(__file__),
)


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    return identity.run_identity_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        repo_root=Path(__file__).resolve().parents[3],
        git_sha_fn=identity.git_sha,
        git_dirty_fn=identity.git_dirty,
    )
