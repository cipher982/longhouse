"""The iOS merge gate is split across VMs; the split must still run every test once."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/run_ios_tests.sh"


def merge_schemes() -> set[str]:
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"^IOS_MERGE_TEST_SCHEMES \?= (.+)$", makefile, re.MULTILINE)
    assert match, "Makefile no longer defines IOS_MERGE_TEST_SCHEMES"
    return set(match.group(1).split())


def ci_lanes() -> list[tuple[list[str], list[tuple[str, str, str]]]]:
    """(schemes, [(scheme, only|skip, id)]) for every entry of the ios-tests matrix."""
    workflow = (ROOT / ".github/workflows/contract-first-ci.yml").read_text()
    job = workflow.split("\n  ios-tests:\n", 1)[1].split("\n  wheel-package:", 1)[0]
    entries = re.findall(
        r"^\s*- lane: .*\n\s+schemes: \"?([^\"\n]+?)\"?\n\s+filter: \"([^\"]*)\"", job, re.M
    )
    assert entries, "the ios-tests job must fan out over `matrix.include` lanes"
    lanes = []
    for schemes, filter_ in entries:
        parsed = []
        for entry in filter_.split():
            scheme, mode, identifier = entry.split(":", 2)
            parsed.append((scheme, mode, identifier))
        lanes.append((schemes.split(), parsed))
    return lanes


def plan_skipped(scheme: str) -> list[str]:
    """Full test ids that the scheme's test plan skips (`Target/Class/method`)."""
    project = (ROOT / "ios/XcodeHarness/project.yml").read_text()
    match = re.search(rf"^  {scheme}:\n(?:    .*\n|\n)*?      testPlans:\n        - path: (\S+)", project, re.M)
    if not match:
        return []
    plan = json.loads((ROOT / "ios/XcodeHarness" / match.group(1)).read_text())
    return [
        f"{target['target']['name']}/{skipped}"
        for target in plan["testTargets"]
        for skipped in target.get("skippedTests", [])
    ]


def excludes(skips: list[str], test_id: str) -> bool:
    return any(test_id == skip or test_id.startswith(skip + "/") for skip in skips)


class CiCoversTheMergeGateTests(unittest.TestCase):
    def test_the_smoke_scheme_test_plan_is_found(self):
        # plan_skipped() reads project.yml with a regex; a reformat must fail here
        # instead of turning every plan check below into a pass.
        self.assertEqual(len(plan_skipped("LonghouseSmoke")), 4)

    def test_selected_classes_exist(self):
        sources = "\n".join(
            path.read_text() for path in (ROOT / "ios/Tests").rglob("*.swift")
        )
        for _, filters in ci_lanes():
            for _, mode, identifier in filters:
                if mode == "only":
                    name = identifier.split("/")[-1]
                    self.assertRegex(sources, rf"class {name}\b", f"no class {name} to select")

    def test_every_merge_scheme_runs_whole_or_as_a_complementary_pair(self):
        lanes = ci_lanes()
        schemes = merge_schemes()
        self.assertEqual({name for names, _ in lanes for name in names}, schemes)
        for names, filters in lanes:
            for scheme, _, _ in filters:
                self.assertIn(scheme, names, f"filter for {scheme} in a lane that does not run it")
        for scheme in schemes:
            runs = [
                [(mode, identifier) for name, mode, identifier in filters if name == scheme]
                for names, filters in lanes
                if scheme in names
            ]
            plan = plan_skipped(scheme)
            if len(runs) == 1:
                # Whole means nothing but the plan's own skips, repeated here.
                self.assertTrue(
                    all(mode == "skip" and identifier in plan for mode, identifier in runs[0]),
                    f"{scheme}: its only lane leaves tests out: {runs[0]}",
                )
                continue
            self.assertEqual(len(runs), 2, f"{scheme}: run by {len(runs)} lanes")
            # A class (or target) is a split point; a method-level skip is not.
            def split_points(run, mode):
                return sorted(i for m, i in run if m == mode and i.count("/") <= 1)

            only = [r for r in runs if split_points(r, "only")]
            skip = [r for r in runs if split_points(r, "skip")]
            self.assertEqual(len(only), 1, f"{scheme}: needs exactly one `only` lane: {runs}")
            self.assertEqual(len(skip), 1, f"{scheme}: needs exactly one `skip` lane: {runs}")
            self.assertEqual(
                split_points(only[0], "only"),
                split_points(skip[0], "skip"),
                f"{scheme}: the `only` and `skip` lanes must name the same classes, or tests drop out",
            )
            for run in runs:
                for mode, identifier in run:
                    if mode == "skip" and identifier.count("/") > 1:
                        self.assertIn(
                            identifier,
                            plan,
                            f"{scheme}: skipping {identifier} drops a test the plan runs",
                        )

    def test_command_line_filters_replace_the_test_plans_skips_so_every_lane_repeats_them(self):
        """xcodebuild ignores a plan's skippedTests once -only/-skip-testing is given.

        The lane that skips a class ran the plan-skipped InboxCapture tests, and the lane
        that selected a class ran the plan-skipped SessionChat capture tests (run
        36653276780), until each lane named them itself.
        """
        for names, filters in ci_lanes():
            for scheme in names:
                skips = [i for name, mode, i in filters if name == scheme and mode == "skip"]
                onlys = [i for name, mode, i in filters if name == scheme and mode == "only"]
                if not any(name == scheme for name, _, _ in filters):
                    continue  # unfiltered: xcodebuild applies the plan
                for test_id in plan_skipped(scheme):
                    if onlys and not excludes(onlys, test_id):
                        continue  # never selected by this lane
                    self.assertTrue(
                        excludes(skips, test_id),
                        f"{scheme}: the plan skips {test_id} but this lane's filter would run it",
                    )


class RunnerFiltersTests(unittest.TestCase):
    """run_ios_tests.sh turns IOS_TEST_FILTER into xcodebuild arguments, checked against a stub."""

    def run_script(self, schemes: str, filter_: str) -> subprocess.CompletedProcess:
        # The hosted-VM boundary check comes first; this exercises what follows it.
        script = SCRIPT.read_text()
        body = re.sub(r"if ! python3 .*test_boundary\.py\"; then.*?\nfi\n", "", script, flags=re.S)
        with tempfile.TemporaryDirectory() as scratch:
            stub = Path(scratch) / "xcodebuild"
            stub.write_text('#!/bin/bash\necho "XCODEBUILD $*"\n')
            stub.chmod(0o755)
            return subprocess.run(
                ["bash", "-c", body, "run_ios_tests.sh", "platform=iOS Simulator,id=X"],
                env={
                    "PATH": f"{scratch}:/usr/bin:/bin",
                    "HOME": scratch,
                    "IOS_DERIVED_DATA_PATH": f"{scratch}/derived-data",
                    "IOS_TEST_SCHEMES": schemes,
                    "IOS_TEST_FILTER": filter_,
                },
                capture_output=True,
                text=True,
            )

    def invocations(self, result: subprocess.CompletedProcess) -> dict[tuple[str, str], str]:
        """{(scheme, action): the stub's argument line} for the two xcodebuild actions."""
        found = {}
        for line in result.stdout.splitlines():
            if not line.startswith("XCODEBUILD "):
                continue
            scheme = re.search(r"-scheme (\S+)", line).group(1)
            action = "build" if line.endswith("build-for-testing") else "test"
            found[(scheme, action)] = line
        return found

    def test_only_and_skip_become_the_matching_xcodebuild_flags(self):
        klass = "LonghouseIOSUITests/SessionChatUITests"
        skip = self.invocations(self.run_script("Longhouse LonghouseSmoke", f"LonghouseSmoke:skip:{klass}"))
        self.assertIn(f"-skip-testing:{klass}", skip[("LonghouseSmoke", "test")])
        self.assertNotIn("-only-testing", skip[("LonghouseSmoke", "test")])
        only = self.invocations(self.run_script("LonghouseSmoke", f"LonghouseSmoke:only:{klass}"))
        self.assertIn(f"-only-testing:{klass}", only[("LonghouseSmoke", "test")])
        self.assertNotIn("-skip-testing", only[("LonghouseSmoke", "test")])

    def test_a_filter_reaches_only_its_own_scheme_and_only_the_test_action(self):
        klass = "LonghouseIOSUITests/SessionChatUITests"
        runs = self.invocations(self.run_script("Longhouse LonghouseSmoke", f"LonghouseSmoke:skip:{klass}"))
        self.assertEqual(len(runs), 4)
        for key, line in runs.items():
            if key != ("LonghouseSmoke", "test"):
                self.assertNotIn("-testing:", line, key)

    def test_no_filter_runs_every_scheme_whole(self):
        runs = self.invocations(self.run_script("Longhouse LonghouseSmoke", ""))
        self.assertEqual(len(runs), 4)
        self.assertFalse(any("-testing:" in line for line in runs.values()))

    def test_a_filter_for_a_scheme_the_run_does_not_build_is_refused(self):
        result = self.run_script("Longhouse", "LonghouseSmoke:only:X")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not run", result.stderr)

    def test_a_malformed_filter_is_refused(self):
        result = self.run_script("LonghouseSmoke", "LonghouseSmoke:bogus:X")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must look like", result.stderr)


if __name__ == "__main__":
    unittest.main()
