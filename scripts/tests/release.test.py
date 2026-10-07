#!/usr/bin/env python3
"""Regression contracts for the single-command release ceremony."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "scripts" / "ops" / "release.sh").read_text(encoding="utf-8")


def test_full_validation_gates_candidate_push() -> None:
    bump = SOURCE.index("bump-my-version bump")
    commit = SOURCE.index('git -C "$ROOT" commit')
    validation = SOURCE.index("make test-ci'")
    push = SOURCE.index('git -C "$ROOT" push')

    assert bump < commit < validation < push


def test_the_lock_is_kept_like_every_other_holder() -> None:
    assert 'lockf -k "$HEAVY_BUILD_LOCK"' in SOURCE


def test_only_the_validation_holds_the_heavy_build_lock() -> None:
    validation = SOURCE.index("run_heavy bash -c 'cd \"$1\" && make test-ci'")
    gates = SOURCE.index('echo "Waiting for pre-release exact-SHA gates')

    # The lock is taken by the validation step alone: never around the gate,
    # publish, or notarization waits that follow it.
    assert SOURCE.count("run_heavy ") == 1  # the one call; its definition is run_heavy()
    assert validation < gates
    assert "heavy_lock_held_by_ancestor" in SOURCE  # an outer lockf wrapper must not deadlock us


def test_a_resume_skips_validation_the_exact_commit_already_passed() -> None:
    stamp = SOURCE.index('VALIDATED_STAMP="$ROOT/.build/release-validated/$BUMP_SHA"')
    validation = SOURCE.index("run_heavy bash -c 'cd \"$1\" && make test-ci'")
    clean_check = SOURCE.index("Release validation changed tracked files")
    write = SOURCE.index('> "$VALIDATED_STAMP"')
    push = SOURCE.index('git -C "$ROOT" push')

    # Keyed by commit SHA, written only after test-ci returned (set -e) AND the
    # validation left the tree clean, and the candidate push comes after all of it.
    assert stamp < validation < clean_check < write < push
    assert 'RELEASE_REVALIDATE' in SOURCE


def test_validation_guest_is_sized_for_the_laptop_not_cubes_shared_pods() -> None:
    assert "docker_cpus < 8 ? docker_cpus : 8" in SOURCE  # never more CPUs than the Docker VM has
    assert 'LONGHOUSE_TEST_MEMORY="${LONGHOUSE_TEST_MEMORY:-8g}"' in SOURCE


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
    for skipped_check in ("--skip-release", "--skip-public-package", "--skip-runtime-artifacts", "--skip-demo"):
        assert skipped_check in gate
    assert "--skip-live" not in gate


def test_final_launch_readiness_also_skips_only_the_demo() -> None:
    readiness = SOURCE.index('echo "Verifying launch readiness for $BUMP_SHA')
    shipped = SOURCE.index('echo ""\necho "Release $VERSION shipped')
    final_check = SOURCE[readiness:shipped]

    assert "--skip-demo" in final_check
    assert "--skip-live" not in final_check


def test_release_dispatches_only_path_filtered_gates_missing_for_exact_sha() -> None:
    push = SOURCE.index('git -C "$ROOT" push')
    readiness = SOURCE.index('echo "Waiting for pre-release exact-SHA gates')
    dispatch = SOURCE[push:readiness]

    assert "for workflow in runtime-image.yml deploy-and-verify.yml launch-gate.yml" in dispatch
    assert "test-install.yml" not in dispatch
    assert '--commit "$BUMP_SHA"' in dispatch
    assert 'if [[ "$run_count" == "0" ]]' in dispatch
    assert 'gh workflow run "$workflow"' in dispatch


def test_a_concurrent_push_cannot_kill_the_release_but_a_real_failure_still_does() -> None:
    gate_start = SOURCE.index('echo "Waiting for pre-release exact-SHA gates')
    release_create = SOURCE.index("gh release create")
    readiness = SOURCE.index('echo "Verifying launch readiness for $BUMP_SHA')
    shipped = SOURCE.index('echo ""\necho "Release $VERSION shipped')

    # Both waits take coverage for identity and rerun an uncovered superseded run, bounded.
    for gate in (SOURCE[gate_start:release_create], SOURCE[readiness:shipped]):
        assert "--accept-covering --redispatch-superseded 2" in gate
    readiness_source = (ROOT / "scripts" / "ops" / "launch-readiness.py").read_text(encoding="utf-8")
    assert 'conclusion == "cancelled"' in readiness_source  # only a cancellation is ever covered or rerun


def test_same_version_resumes_a_pushed_candidate() -> None:
    assert 'if [[ "$CURRENT_VERSION" == "$PYVER" ]]; then' in SOURCE
    assert "reusing the current candidate" in SOURCE
    assert 'git -C "$ROOT" merge-base --is-ancestor "$REMOTE_HEAD" "$LOCAL_HEAD"' in SOURCE
    assert "VERSION_MARKERS=(" in SOURCE


def test_release_fetches_remote_branch_before_building_changelog() -> None:
    fetch = SOURCE.index("fetch --quiet origin main")
    previous_tag = SOURCE.index("PREV_TAG=")

    assert fetch < previous_tag


if __name__ == "__main__":
    test_full_validation_gates_candidate_push()
    test_the_lock_is_kept_like_every_other_holder()
    test_only_the_validation_holds_the_heavy_build_lock()
    test_a_resume_skips_validation_the_exact_commit_already_passed()
    test_validation_guest_is_sized_for_the_laptop_not_cubes_shared_pods()
    test_pre_release_gate_precedes_github_release_and_skips_only_release_evidence()
    test_final_launch_readiness_also_skips_only_the_demo()
    test_release_dispatches_only_path_filtered_gates_missing_for_exact_sha()
    test_a_concurrent_push_cannot_kill_the_release_but_a_real_failure_still_does()
    test_same_version_resumes_a_pushed_candidate()
    test_release_fetches_remote_branch_before_building_changelog()
    print("release tests passed")
