#!/usr/bin/env python3
"""A later green Terminal Truth run covers a commit with no verdict only when it descends from it and is on main."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("terminal_truth_cover", ROOT / "scripts" / "ops" / "terminal_truth_cover.py")
cover = importlib.util.module_from_spec(spec)
sys.modules["terminal_truth_cover"] = cover
spec.loader.exec_module(cover)

OK = {"context": "longhouse/terminal-truth", "state": "success", "creator": {"login": "github-actions[bot]"}, "target_url": "u"}


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name

        def git(*args):
            return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                                  capture_output=True, text=True, check=True).stdout.strip()

        git("init", "-q", "-b", "main")
        shas = {}
        for name in ("a", "b", "c"):
            git("commit", "-q", "--allow-empty", "-m", name)
            shas[name] = git("rev-parse", "HEAD")
        git("checkout", "-q", "-b", "side", shas["a"])
        git("commit", "-q", "--allow-empty", "-m", "side")
        shas["side"] = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        self.s = shas

    def runs(self, *shas, conclusion="success"):
        return [{"head_sha": s, "run_number": n, "conclusion": conclusion} for n, s in enumerate(shas, 1)]

    def find(self, commit, runs, statuses):
        return cover.covering(self.repo, "main", commit, runs, lambda sha: statuses.get(sha, []))

    def test_a_descendant_with_a_passing_status_covers(self):
        self.assertEqual(self.find(self.s["a"], self.runs(self.s["c"]), {self.s["c"]: [OK]}), (self.s["c"], "u"))

    def test_a_commit_that_is_not_a_descendant_does_not_cover(self):
        self.assertIsNone(self.find(self.s["b"], self.runs(self.s["a"]), {self.s["a"]: [OK]}))

    def test_a_commit_off_main_does_not_cover(self):
        self.assertIsNone(self.find(self.s["a"], self.runs(self.s["side"]), {self.s["side"]: [OK]}))

    def test_a_status_that_is_not_a_github_actions_success_does_not_cover(self):
        for row in ({**OK, "creator": {"login": "x"}}, {**OK, "state": "failure"}, {**OK, "state": "pending"}):
            self.assertIsNone(self.find(self.s["a"], self.runs(self.s["c"]), {self.s["c"]: [row]}))

    def test_the_newest_status_of_the_covering_commit_decides(self):
        self.assertIsNone(self.find(self.s["a"], self.runs(self.s["c"]), {self.s["c"]: [{**OK, "state": "failure"}, OK]}))

    def test_a_run_that_did_not_succeed_is_ignored(self):
        self.assertIsNone(self.find(self.s["a"], self.runs(self.s["c"], conclusion="cancelled"), {self.s["c"]: [OK]}))

    def test_the_commit_itself_is_not_its_own_cover(self):
        self.assertIsNone(self.find(self.s["a"], self.runs(self.s["a"]), {self.s["a"]: [OK]}))


if __name__ == "__main__":
    unittest.main()
