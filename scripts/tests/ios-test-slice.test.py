"""The iOS merge gate is split across VMs; the split must still cover every test."""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ios_test_slice", ROOT / "scripts/ci/ios_test_slice.py"
)
ios_slice = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ios_slice
spec.loader.exec_module(ios_slice)

ENUMERATION = {
    "errors": [],
    "values": [
        {
            "enabledTests": [{"identifier": f"Target/Class{k // 3}/test{k}()"} for k in (5, 3, 0, 1, 4, 2, 6)],
            "disabledTests": [{"identifier": "Target/Class0/testSkipped()"}],
        }
    ],
}


def ci_lanes() -> list[tuple[str, str]]:
    """(scheme, slice) for every entry of the ios-tests matrix; slice may be empty."""
    workflow = (ROOT / ".github/workflows/contract-first-ci.yml").read_text()
    job = workflow.split("\n  ios-tests:\n", 1)[1].split("\n  wheel-package:", 1)[0]
    entries = re.findall(r"^\s*- lane: .*\n\s+schemes: (\S+)\n\s+slice: \"([^\"]*)\"", job, re.M)
    assert entries, "the ios-tests job must fan out over `matrix.include` lanes"
    return entries


def merge_schemes() -> set[str]:
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"^IOS_MERGE_TEST_SCHEMES \?= (.+)$", makefile, re.MULTILINE)
    assert match, "Makefile no longer defines IOS_MERGE_TEST_SCHEMES"
    return set(match.group(1).split())


class SliceTests(unittest.TestCase):
    def test_slices_partition_the_enabled_tests(self):
        everything = [f"-only-testing:Target/Class{k // 3}/test{k}()" for k in range(7)]
        for count in range(1, 5):
            picked = [
                argument
                for index in range(1, count + 1)
                for argument in ios_slice.slice_arguments(ENUMERATION, f"{index}/{count}")
            ]
            self.assertEqual(sorted(picked), sorted(everything), count)

    def test_skipped_tests_are_never_selected(self):
        arguments = ios_slice.slice_arguments(ENUMERATION, "1/1")
        self.assertFalse(any("testSkipped" in argument for argument in arguments))

    def test_a_slice_that_misreads_the_enumeration_fails_instead_of_passing_empty(self):
        with self.assertRaises(ValueError):
            ios_slice.slice_arguments({"values": []}, "1/2")
        with self.assertRaises(ValueError):
            ios_slice.slice_arguments({"errors": ["boom"], "values": []}, "1/2")

    def test_malformed_slices_are_refused(self):
        for bad in ("1", "0/2", "3/2", "a/b", "1/0"):
            with self.assertRaises(ValueError, msg=bad):
                ios_slice.parse_slice(bad)


class CiCoversTheMergeGateTests(unittest.TestCase):
    def test_the_workflow_lanes_run_every_merge_scheme_completely(self):
        lanes = ci_lanes()
        self.assertEqual({scheme for scheme, _ in lanes}, merge_schemes())
        for scheme in merge_schemes():
            slices = sorted(slice_ for name, slice_ in lanes if name == scheme)
            if slices == [""]:
                continue
            counts = {slice_.split("/")[1] for slice_ in slices if slice_}
            self.assertEqual(len(counts), 1, f"{scheme}: slices disagree on n: {slices}")
            n = int(counts.pop())
            self.assertEqual(
                slices,
                sorted(f"{i}/{n}" for i in range(1, n + 1)),
                f"{scheme}: an unsliced lane or a missing slice would drop or repeat tests",
            )


if __name__ == "__main__":
    unittest.main()
