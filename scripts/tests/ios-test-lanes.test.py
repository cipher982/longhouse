"""The iOS merge gate is split across VMs; the split must still run every test once."""

from __future__ import annotations

import re
import subprocess
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


class CiCoversTheMergeGateTests(unittest.TestCase):
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
            if len(runs) == 1:
                self.assertEqual(runs[0], [], f"{scheme}: its only lane filters tests out")
                continue
            self.assertEqual(len(runs), 2, f"{scheme}: run by {len(runs)} lanes")
            only = [r for r in runs if r and all(mode == "only" for mode, _ in r)]
            skip = [r for r in runs if r and all(mode == "skip" for mode, _ in r)]
            self.assertEqual(len(only), 1, f"{scheme}: needs exactly one `only` lane: {runs}")
            self.assertEqual(len(skip), 1, f"{scheme}: needs exactly one `skip` lane: {runs}")
            self.assertEqual(
                sorted(identifier for _, identifier in only[0]),
                sorted(identifier for _, identifier in skip[0]),
                f"{scheme}: the `only` and `skip` lanes must name the same tests, or tests drop out",
            )


class RunnerRefusesBadFiltersTests(unittest.TestCase):
    """run_ios_tests.sh validates IOS_TEST_FILTER before it touches xcodebuild."""

    def refusal(self, schemes: str, filter_: str) -> subprocess.CompletedProcess:
        # The hosted-VM boundary check comes first; this exercises what follows it.
        script = SCRIPT.read_text()
        body = re.sub(r"if ! python3 .*test_boundary\.py\"; then.*?\nfi\n", "", script, flags=re.S)
        return subprocess.run(
            ["bash", "-c", body, "run_ios_tests.sh", "platform=iOS Simulator,id=X"],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "IOS_DERIVED_DATA_PATH": "/tmp/ios-test-lanes-nonexistent-derived-data",
                "IOS_TEST_SCHEMES": schemes,
                "IOS_TEST_FILTER": filter_,
            },
            capture_output=True,
            text=True,
        )

    def test_a_filter_for_a_scheme_the_run_does_not_build_is_refused(self):
        result = self.refusal("Longhouse", "LonghouseSmoke:only:X")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not run", result.stderr)

    def test_a_malformed_filter_is_refused(self):
        result = self.refusal("LonghouseSmoke", "LonghouseSmoke:bogus:X")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must look like", result.stderr)


if __name__ == "__main__":
    unittest.main()
