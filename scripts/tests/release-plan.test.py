#!/usr/bin/env python3
"""release_plan.py: the dogfood gates the Release workflow keeps, against a real temporary git repo."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
spec = importlib.util.spec_from_file_location("release_plan", ROOT / "scripts" / "ops" / "release_plan.py")
rp = importlib.util.module_from_spec(spec)
sys.modules["release_plan"] = rp
spec.loader.exec_module(rp)

OK = {"context": "longhouse/terminal-truth", "state": "success", "creator": {"login": "github-actions[bot]"}, "target_url": "u"}


class PlanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.shas: dict[str, str] = {}

        def commit(name, *files):
            for f in files:
                (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
                (self.repo / f).write_text(name)
            self.git("add", "-A")
            self.git("commit", "-q", "--allow-empty", "-m", name)
            self.shas[name] = self.git("rev-parse", "HEAD")

        self.git("init", "-q", "-b", "main")
        commit("base", "README.md")
        commit("docs", "docs/a.md")
        commit("web", "web/src/a.ts")
        commit("auth", "server/zerg/auth/x.py")
        commit("terminal", "server/zerg/services/terminal_text.py")
        patches = [
            mock.patch.object(rp, "ROOT", self.repo),
            mock.patch.object(rp, "FILTERS", ROOT / ".github" / "path-filters.yml"),
            mock.patch.object(rp, "ci_green", lambda sha: True),
            mock.patch.object(rp, "served_commit", lambda: self.served),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.served = self.shas["base"]

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_red_ci_never_deploys(self):
        with mock.patch.object(rp, "ci_green", lambda sha: False):
            self.assertEqual(rp.plan(self.shas["web"], True, False)["deploy"], "false")

    def test_canary_only_needs_no_dogfood(self):
        self.served = None
        result = rp.plan(self.shas["web"], False, False)
        self.assertEqual((result["deploy"], result["sensitive"]), ("true", "false"))

    def test_an_unreadable_dogfood_is_an_error_not_a_guess(self):
        self.served = None
        with self.assertRaises(RuntimeError):
            rp.plan(self.shas["web"], True, False)

    def test_already_served_and_docs_only_do_nothing(self):
        self.served = self.shas["web"]
        self.assertEqual(rp.plan(self.shas["web"], True, False)["deploy"], "false")
        self.served = self.shas["base"]
        self.assertEqual(rp.plan(self.shas["docs"], True, False)["deploy"], "false")

    def test_never_backwards_unless_rollback(self):
        self.served = self.shas["web"]
        self.assertEqual(rp.plan(self.shas["docs"], True, False)["deploy"], "false")
        back = rp.plan(self.shas["docs"], True, True)
        self.assertEqual((back["deploy"], back["sensitive"]), ("true", "true"))

    def test_a_runtime_change_in_a_non_sensitive_range_is_not_qa_gated(self):
        self.served = self.shas["docs"]
        result = rp.plan(self.shas["web"], True, False)
        self.assertEqual((result["deploy"], result["sensitive"], result["tt_commit"]), ("true", "false", ""))

    def test_a_sensitive_path_takes_the_qa_gate(self):
        result = rp.plan(self.shas["auth"], True, False)
        self.assertEqual((result["deploy"], result["sensitive"]), ("true", "true"))

    def test_runtime_image_globs_come_from_the_path_filters(self):
        globs = rp.runtime_image_globs()
        self.assertIn("server/**", globs)
        self.assertIn("scripts/ops/release-artifacts.py", globs)
        self.assertNotIn("docs/**", globs)


class TerminalTruthTests(unittest.TestCase):
    def state(self, rows, covered=None):
        with mock.patch.object(rp, "gh_json", lambda *a: rows if "statuses" in a[1] else {"workflow_runs": []}), \
             mock.patch.object(rp.cover, "covering", lambda *a, **k: covered):
            return rp.terminal_truth_state("a" * 40)

    def test_a_github_actions_success_passes(self):
        self.assertEqual(self.state([OK])[0], 0)

    def test_failed_errored_or_foreign_statuses_refuse_even_when_covered(self):
        for row in ({**OK, "state": "failure"}, {**OK, "state": "error"}, {**OK, "creator": {"login": "someone"}}):
            self.assertEqual(self.state([row], covered=("b" * 40, "u"))[0], rp.REFUSED)

    def test_missing_or_pending_waits_unless_a_later_commit_covers_it(self):
        self.assertEqual(self.state([])[0], rp.WAITING)
        self.assertEqual(self.state([{**OK, "state": "pending"}])[0], rp.WAITING)
        self.assertEqual(self.state([], covered=("b" * 40, "u"))[0], 0)

    def test_wait_gives_up_at_the_deadline(self):
        with mock.patch.object(rp, "terminal_truth_state", lambda c: (rp.WAITING, "pending")), mock.patch.object(rp.time, "sleep"):
            self.assertEqual(rp.wait_terminal_truth("a" * 40, 0), rp.WAITING)


if __name__ == "__main__":
    unittest.main()
