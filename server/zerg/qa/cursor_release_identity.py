"""Strict identity qualification for an exact Cursor Agent executable."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zerg.qa import provider_release_identity as identity

PROFILE = "cursor_release_identity_v1"
SCENARIO_ID = "cursor_release_identity"
ASSERTIONS = identity.ASSERTIONS
OBSERVED_INSTALL_PROFILE = "cursor_observed_install_v1"
OBSERVED_INSTALL_SCENARIO_ID = "cursor_observed_install"
OBSERVED_INSTALL_ASSERTIONS = ("cursor_observed_install_contract_preserved",)
# cursor-agent prints a bare calendar build such as 2026.07.23-e383d2b.
VERSION_LINE = identity.CALENDAR_BUILD
_PROFILE = identity.IdentityProfile(
    provider="cursor",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=VERSION_LINE,
    oracle_source=Path(__file__),
    version_grammar=identity.CALENDAR_BUILD,
)
_OBSERVED_INSTALL_PROFILE = identity.IdentityProfile(
    provider="cursor",
    profile=OBSERVED_INSTALL_PROFILE,
    scenario_id=OBSERVED_INSTALL_SCENARIO_ID,
    version_line=VERSION_LINE,
    oracle_source=Path(__file__),
    version_grammar=identity.CALENDAR_BUILD,
)


def run_observed_install(request_path: Path, output_root: Path) -> dict[str, Any]:
    return identity.run_identity_profile(
        request_path,
        output_root,
        profile=_OBSERVED_INSTALL_PROFILE,
        repo_root=Path(__file__).resolve().parents[3],
        git_sha_fn=identity.git_sha,
        git_dirty_fn=identity.git_dirty,
    )


run_observed_install.SCENARIO_ID = OBSERVED_INSTALL_SCENARIO_ID  # type: ignore[attr-defined]


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    return identity.run_identity_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        repo_root=Path(__file__).resolve().parents[3],
        git_sha_fn=identity.git_sha,
        git_dirty_fn=identity.git_dirty,
    )
