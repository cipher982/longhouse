#!/usr/bin/env python3
"""Regression contracts for the single-command release ceremony."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "scripts" / "ops" / "release.sh").read_text(encoding="utf-8")


def test_full_validation_gates_candidate_push() -> None:
    bump = SOURCE.index("bump-my-version bump")
    commit = SOURCE.index('git -C "$ROOT" commit')
    validation = SOURCE.index("(cd \"$ROOT\" && make test-ci)")
    push = SOURCE.index('git -C "$ROOT" push')

    assert bump < commit < validation < push


def test_pre_release_gate_precedes_github_release_and_skips_only_release_evidence() -> None:
    gate_start = SOURCE.index('echo "Waiting for pre-release exact-SHA gates')
    release_create = SOURCE.index("gh release create")
    gate = SOURCE[gate_start:release_create]

    for workflow in (
        '"CI"',
        '"Deploy and Verify"',
        '"Launch Gate"',
        '"Installer Validation Ring"',
        '"Hosted Live QA"',
    ):
        assert f"--required-workflow {workflow}" in gate
    for skipped_check in ("--skip-release", "--skip-public-package", "--skip-runtime-artifacts"):
        assert skipped_check in gate
    assert "--skip-live" not in gate


def test_same_version_resumes_a_pushed_candidate() -> None:
    assert 'if [[ "$CURRENT_VERSION" == "$PYVER" ]]; then' in SOURCE
    assert "reusing the current candidate" in SOURCE
    assert 'git -C "$ROOT" merge-base --is-ancestor "$REMOTE_HEAD" "$LOCAL_HEAD"' in SOURCE
    assert 'VERSION_MARKERS=(' in SOURCE


def test_release_fetches_tags_before_building_changelog() -> None:
    fetch = SOURCE.index('fetch --quiet --tags origin main')
    previous_tag = SOURCE.index("PREV_TAG=")

    assert fetch < previous_tag


if __name__ == "__main__":
    test_full_validation_gates_candidate_push()
    test_pre_release_gate_precedes_github_release_and_skips_only_release_evidence()
    test_same_version_resumes_a_pushed_candidate()
    test_release_fetches_tags_before_building_changelog()
    print("release tests passed")
