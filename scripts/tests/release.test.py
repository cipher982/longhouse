#!/usr/bin/env python3
"""Regression contracts for the single-command release ceremony."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "scripts" / "ops" / "release.sh").read_text(encoding="utf-8")
HEAVY_LOCK_LIB = (ROOT / "scripts" / "lib" / "heavy-build-lock.sh").read_text(encoding="utf-8")


def test_full_validation_gates_candidate_push() -> None:
    bump = SOURCE.index("bump-my-version bump")
    commit = SOURCE.index('git -C "$ROOT" commit')
    validation = SOURCE.index("make test-ci'")
    push = SOURCE.index('git -C "$ROOT" push')

    assert bump < commit < validation < push


def test_the_lock_is_kept_like_every_other_holder() -> None:
    assert '. "$ROOT/scripts/lib/heavy-build-lock.sh"' in SOURCE
    assert 'lockf -k "$HEAVY_BUILD_LOCK"' in HEAVY_LOCK_LIB


def test_only_the_validation_holds_the_heavy_build_lock() -> None:
    validation = SOURCE.index("run_heavy bash -c 'cd \"$1\" && make test-ci'")
    gates = SOURCE.index('echo "Waiting for pre-release exact-SHA gates')

    # The lock is taken by the validation step alone: never around the gate,
    # publish, or notarization waits that follow it.
    assert SOURCE.count("run_heavy ") == 1  # the one call (lh_run_heavy, from scripts/lib/heavy-build-lock.sh)
    assert validation < gates
    assert "heavy_lock_held_by_ancestor" in HEAVY_LOCK_LIB  # an outer lockf wrapper must not deadlock us


def test_a_resume_skips_validation_the_exact_commit_already_passed() -> None:
    stamp = SOURCE.index('VALIDATED_STAMP="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/release-validated/$BUMP_SHA"')
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


# --- the whole ceremony against a fixture origin: real release.sh, ring lock and exact checkout ---

VERSION_FILES = {
    "server/pyproject.toml": '[project]\nname = "fixture"\nversion = "{v}"\n',
    "engine/Cargo.toml": '[package]\nname = "fixture"\nversion = "{v}"\n',
    "runner/package.json": '{{\n  "version": "{v}"\n}}\n',
    "ios/XcodeHarness/Configs/Version.xcconfig": "MARKETING_VERSION = {v}\n",
    ".bumpversion.toml": '[tool.bumpversion]\ncurrent_version = "{v}"\n',
    "server/uv.lock": "lock {v}\n",
    "engine/Cargo.lock": "lock {v}\n",
}

STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
with open(root / "calls", "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "bump-my-version":
    new = args[args.index("--new-version") + 1]
    for path in os.environ["FIXTURE_VERSION_FILES"].split(":"):
        text = pathlib.Path(path).read_text()
        pathlib.Path(path).write_text(re.sub(r"0\.1\.1", new, text))
elif name == "gh":
    if args[:2] == ["run", "list"] and "--jq" in args and args[args.index("--jq") + 1] == "length":
        print(1)
    elif args[:2] == ["run", "list"]:
        print(json.dumps({"databaseId": 7, "status": "completed", "conclusion": "success"}))
    elif args[:2] == ["release", "view"]:
        v = args[2].lstrip("v")
        for asset in (f"longhouse-{v}-py3-none-any.whl", "longhouse-engine-darwin-arm64", "longhouse-engine-linux-x64",
                      "Longhouse-macos-arm64.dmg", "local-runtime-macos-packaging.json"):
            print(asset)
    elif args[:2] == ["release", "download"]:
        print(json.dumps({"notarization_status": "notarized", "public_download_notarization_status": "notarized"}))
elif name == "docker":
    sys.exit(1)
"""

LANE_STUBS = {
    # The lane runs whatever the fixture origin holds: the real release.sh and libraries, stubs for the rest.
    "scripts/ops/review_gate.py": "#!/usr/bin/env python3\n",
    "scripts/ops/launch-readiness.py": "#!/usr/bin/env python3\nimport sys, os\n"
                                       "open(os.environ['FIXTURE_ROOT'] + '/readiness', 'a').write(' '.join(sys.argv[1:3]) + '\\n')\n",
    "Makefile": "test-ci:\n"
                "\tgit rev-parse HEAD >> $(FIXTURE_ROOT)/validated\n"
                "\tpwd >> $(FIXTURE_ROOT)/validated_in\n"
                "\tpython3 scripts/ops/ring_lock.py status release >> $(FIXTURE_ROOT)/lock_during\n"
                "\ttest \"$(FIXTURE_TESTCI_FAIL)\" != 1\n",
}


class ReleaseFixture:
    def __init__(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="longhouse-release-fixture-")
        self.root = Path(self.tmp.name)
        self.origin, self.work, self.lanes, self.bin = (self.root / n for n in ("origin.git", "work", "lanes", "bin"))
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.root, "clone", "-q", str(self.origin), str(self.work))
        self.git(self.work, "config", "user.email", "t@example.com")
        self.git(self.work, "config", "user.name", "t")
        files = {path: body.format(v="0.1.1") for path, body in VERSION_FILES.items()}
        files.update(LANE_STUBS)
        for rel in ("scripts/ops/release.sh", "scripts/ops/ring_lock.py", "scripts/lib/ring-lock.sh",
                    "scripts/lib/exact-checkout.sh", "scripts/lib/heavy-build-lock.sh"):
            files[rel] = (ROOT / rel).read_text()
        for rel, body in files.items():
            path = self.work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            if rel.endswith((".py", ".sh")):
                path.chmod(0o755)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", "fixture")
        self.git(self.work, "push", "-q", "origin", "main")
        self.bin.mkdir()
        for name in ("bump-my-version", "gh", "docker", "uv", "cargo"):
            (self.bin / name).write_text(STUB)
            (self.bin / name).chmod(0o755)

    @staticmethod
    def git(cwd, *args) -> str:
        import subprocess
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def release(self, version="v0.1.2", **env):
        import os, subprocess
        environment = {k: v for k, v in os.environ.items() if not k.startswith(("LH_RING_LOCK", "LONGHOUSE_RELEASE"))}
        environment.update({
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FIXTURE_ROOT": str(self.root),
            # bump-my-version runs in the release checkout, whatever its path: it rewrites relative paths.
            "FIXTURE_VERSION_FILES": ":".join(VERSION_FILES),
            "LONGHOUSE_EXACT_CHECKOUT_PARENT": str(self.lanes),
            "LONGHOUSE_HEAVY_BUILD_LOCK": str(self.root / "heavy.lock"),
            "RELEASE_SETTLE_SECONDS": "0",
            **env,
        })
        return subprocess.run(["bash", str(self.work / "scripts/ops/release.sh"), version], cwd=self.work,
                              env=environment, capture_output=True, text=True, timeout=120)

    def read(self, name) -> list[str]:
        path = self.root / name
        return path.read_text().splitlines() if path.exists() else []

    def gh_calls(self) -> list[list[str]]:
        import json
        return [call[1:] for call in map(json.loads, self.read("calls")) if call[0] == "gh"]

    def assert_nothing_left(self):
        worktrees = self.git(self.work, "worktree", "list", "--porcelain")
        assert worktrees.count("worktree ") == 1, worktrees  # only the checkout the release was started from
        assert not any(self.lanes.iterdir()) if self.lanes.exists() else True, list(self.lanes.iterdir())
        locks = Path(self.git(self.work, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "ring-locks"
        assert not (locks / "release.json").exists(), (locks / "release.json").read_text()

    def close(self):
        self.tmp.cleanup()


def test_release_runs_from_a_disposable_checkout_pushes_the_bump_and_tags_that_exact_sha() -> None:
    f = ReleaseFixture()
    try:
        # The checkout it starts from is on another branch with uncommitted work: neither matters, and both survive.
        f.git(f.work, "checkout", "-q", "-b", "someone-elses-topic")
        (f.work / "server/pyproject.toml").write_text("edited by another agent\n")
        result = f.release()
        assert result.returncode == 0, result.stdout + result.stderr
        bump = f.git(f.origin, "rev-parse", "main")
        assert f.git(f.origin, "log", "-1", "--format=%s", "main") == "Bump version to 0.1.2"
        assert 'version = "0.1.2"' in f.git(f.origin, "show", "main:server/pyproject.toml")
        assert ["release", "create", "v0.1.2", "--target", bump] == f.gh_calls()[[c[:2] for c in f.gh_calls()].index(["release", "create"])][:5]
        assert f.read("validated") == [bump]  # make test-ci ran on the exact candidate
        [lane] = f.read("validated_in")
        assert Path(lane).parent == f.lanes and lane != str(f.work), lane
        lock = "\n".join(f.read("lock_during"))
        assert "release held by" in lock and "make release v0.1.2" in lock and bump[:12] in lock, lock
        assert f.git(f.work, "rev-parse", "--abbrev-ref", "HEAD") == "someone-elses-topic"
        assert (f.work / "server/pyproject.toml").read_text() == "edited by another agent\n"
        assert not f.git(f.work, "for-each-ref", "refs/longhouse/release-candidates/")
        f.assert_nothing_left()
    finally:
        f.close()


def test_a_failed_validation_cleans_up_and_a_rerun_resumes_the_same_candidate() -> None:
    f = ReleaseFixture()
    try:
        failed = f.release(FIXTURE_TESTCI_FAIL="1")
        assert failed.returncode != 0
        before = f.git(f.origin, "rev-parse", "main")
        assert f.git(f.origin, "log", "-1", "--format=%s", "main") == "fixture"  # nothing pushed
        [candidate] = f.read("validated")
        assert f.git(f.work, "rev-parse", "refs/longhouse/release-candidates/v0.1.2") == candidate
        f.assert_nothing_left()

        resumed = f.release()
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert f"Resuming v0.1.2 from its unpushed candidate {candidate[:10]}" in resumed.stdout
        assert f.git(f.origin, "rev-parse", "main") == candidate != before
        assert not f.git(f.work, "for-each-ref", "refs/longhouse/release-candidates/")
        f.assert_nothing_left()
    finally:
        f.close()


def test_a_second_release_refuses_while_one_is_in_flight() -> None:
    import os, subprocess, sys
    f = ReleaseFixture()
    try:
        held = subprocess.run([sys.executable, str(ROOT / "scripts/ops/ring_lock.py"), "--repo", str(f.work), "acquire", "release",
                               "--sha", "a" * 40, "--ttl", "600", "--pid", str(os.getpid()), "--op", "make release v0.1.9"],
                              capture_output=True, text=True)
        assert held.returncode == 0, held.stderr
        result = f.release()
        assert result.returncode == 1
        assert "ring-lock: REFUSED: release held by" in result.stderr and "make release v0.1.9" in result.stderr
        assert f.read("validated") == [] and not (f.lanes.exists() and any(f.lanes.iterdir()))
        assert f.git(f.origin, "log", "-1", "--format=%s", "main") == "fixture"
    finally:
        f.close()


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
    test_release_runs_from_a_disposable_checkout_pushes_the_bump_and_tags_that_exact_sha()
    test_a_failed_validation_cleans_up_and_a_rerun_resumes_the_same_candidate()
    test_a_second_release_refuses_while_one_is_in_flight()
    print("release tests passed")
