#!/usr/bin/env python3
"""Regression contracts for the single-command release ceremony."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "scripts" / "ops" / "release.sh").read_text(encoding="utf-8")
DEPLOY_WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-and-verify.yml").read_text(encoding="utf-8")
HOSTED_QA_WORKFLOW = (ROOT / ".github" / "workflows" / "hosted-live-qa.yml").read_text(encoding="utf-8")


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
    ):
        assert f"--required-workflow {workflow}" in gate
    assert '--required-workflow "Hosted Live QA"' not in gate
    assert "--timeout 7200" in gate
    for skipped_check in ("--skip-release", "--skip-public-package", "--skip-runtime-artifacts"):
        assert skipped_check in gate
    assert "--skip-live" not in gate


def test_release_dispatches_only_path_filtered_gates_missing_for_exact_sha() -> None:
    push = SOURCE.index('git -C "$ROOT" push')
    readiness = SOURCE.index('echo "Waiting for pre-release exact-SHA gates')
    dispatch = SOURCE[push:readiness]

    assert "for workflow in test-install.yml launch-gate.yml" in dispatch
    assert '--commit "$BUMP_SHA"' in dispatch
    assert 'if [[ "$run_count" == "0" ]]' in dispatch
    assert 'gh workflow run "$workflow"' in dispatch


def test_deploy_waits_for_exact_sha_hosted_live_qa() -> None:
    assert "uses: ./.github/workflows/hosted-live-qa.yml" in DEPLOY_WORKFLOW
    assert "source_sha: ${{ needs.gate.outputs.head_sha }}" in DEPLOY_WORKFLOW
    assert "hosted-live-qa.yml/dispatches" not in DEPLOY_WORKFLOW
    assert "workflow_call:" in HOSTED_QA_WORKFLOW
    assert "ref: ${{ inputs.source_sha || github.sha }}" in HOSTED_QA_WORKFLOW


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
    test_release_dispatches_only_path_filtered_gates_missing_for_exact_sha()
    test_deploy_waits_for_exact_sha_hosted_live_qa()
    test_same_version_resumes_a_pushed_candidate()
    test_release_fetches_tags_before_building_changelog()
    print("release tests passed")
