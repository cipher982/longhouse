#!/usr/bin/env python3
"""A main push's path filters diff from the newest green main commit."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("last_green_main", ROOT / "scripts" / "ci" / "last_green_main.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules["last_green_main"] = module
SPEC.loader.exec_module(module)


def fake(green: list[str], head: str = "head"):
    def fetch(url: str) -> dict:
        if url.endswith("/actions/runs/9"):
            return {"workflow_id": 7, "head_sha": head}
        assert "status=success" in url and "branch=main" in url and "event=push" in url
        return {"workflow_runs": [{"head_sha": sha} for sha in green]}

    return fetch


class LastGreenMainTest(unittest.TestCase):
    def test_the_newest_green_commit_is_the_base(self):
        self.assertEqual(module.last_green_main(fake(["g2", "g1"]), "api", "o/r", 9), "g2")

    def test_a_rerun_of_this_commit_is_not_its_own_base(self):
        self.assertEqual(module.last_green_main(fake(["head", "g1"]), "api", "o/r", 9), "g1")

    def test_no_green_run_means_no_base(self):
        self.assertIsNone(module.last_green_main(fake([]), "api", "o/r", 9))


if __name__ == "__main__":
    unittest.main()
