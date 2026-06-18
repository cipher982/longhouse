#!/usr/bin/env python3
"""Schema tests for the provider release-proof coverage map."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_PATH = REPO_ROOT / "docs" / "specs" / "provider-release-proof-coverage.json"

EXPECTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity", "gemini")
EXPECTED_SURFACES = (
    "install/stage exact version",
    "binary identity",
    "auth/status shape",
    "launch managed session",
    "session id/path binding",
    "transcript/log parse",
    "ingest into Longhouse",
    "timeline/session projection",
    "send input",
    "interrupt/abort/steer",
    "reattach/resume",
    "tool/tool-result shape",
    "live-token behavior",
)
REQUIRED_ROW_FIELDS = {
    "provider",
    "surface",
    "covered",
    "test_evidence",
    "proof_boundary",
    "fake_or_real",
    "runs_in_ci",
    "runs_in_sauron_release_watch",
    "accepted_baseline",
    "failure_actionable",
}
ALLOWED_COVERED = {"yes", "partial", "no"}
ALLOWED_FAKE_OR_REAL = {"fake", "real", "mixed", "none"}
# release_proof is intentionally unused until Phase 3 accepts the first real
# provider-release-proof baseline.
ALLOWED_BASELINE = {"none", "parser_fixture", "release_proof"}
ALLOWED_PROOF_BOUNDARY = {
    "none",
    "unsupported",
    "fixture",
    "hermetic",
    "live_no_token",
    "live_no_token_or_fake",
    "hermetic_or_live_no_token",
    "fixture_or_hermetic",
    "path_or_override",
    "real_release_asset",
    "hermetic_plus_manual_live_token",
    "manual_live_token",
    "manual_live_token_or_fake",
}


def _load() -> dict:
    payload = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_coverage_map_has_full_provider_surface_grid() -> None:
    payload = _load()

    assert payload["schema_version"] == 1
    assert tuple(payload["providers"]) == EXPECTED_PROVIDERS
    assert tuple(payload["surfaces"]) == EXPECTED_SURFACES

    rows = payload["rows"]
    assert len(rows) == len(EXPECTED_PROVIDERS) * len(EXPECTED_SURFACES)
    seen = {(row["provider"], row["surface"]) for row in rows}
    assert len(seen) == len(rows)
    assert seen == {
        (provider, surface)
        for provider in EXPECTED_PROVIDERS
        for surface in EXPECTED_SURFACES
    }


def test_coverage_rows_are_auditable() -> None:
    payload = _load()

    for row in payload["rows"]:
        missing = REQUIRED_ROW_FIELDS - set(row)
        assert not missing, f"{row.get('provider')} {row.get('surface')} missing {sorted(missing)}"
        assert row["covered"] in ALLOWED_COVERED, row
        assert row["fake_or_real"] in ALLOWED_FAKE_OR_REAL, row
        assert row["accepted_baseline"] in ALLOWED_BASELINE, row
        assert row["proof_boundary"] in ALLOWED_PROOF_BOUNDARY, row
        assert isinstance(row["test_evidence"], list), row
        assert isinstance(row["runs_in_ci"], bool), row
        assert isinstance(row["runs_in_sauron_release_watch"], bool), row
        assert isinstance(row["failure_actionable"], bool), row

        if row["covered"] != "no" or row["runs_in_ci"] or row["runs_in_sauron_release_watch"]:
            assert row["test_evidence"], f"{row['provider']} {row['surface']} needs evidence"
        if row["covered"] != "no":
            assert row["fake_or_real"] != "none", row
            assert row["proof_boundary"] not in {"none", "unsupported"}, row
        if row["covered"] == "no":
            assert row["accepted_baseline"] == "none", row
            assert not row["runs_in_ci"], row
            assert not row["runs_in_sauron_release_watch"], row
            assert row["proof_boundary"] in {"none", "unsupported"}, row
        if row["runs_in_sauron_release_watch"]:
            assert row["runs_in_ci"], row


def main() -> int:
    tests = [
        test_coverage_map_has_full_provider_surface_grid,
        test_coverage_rows_are_auditable,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
