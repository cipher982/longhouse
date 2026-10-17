#!/usr/bin/env python3
"""The Actions-storage check fails on new uses and on stale allowlist entries."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_actions_storage", ROOT / "scripts" / "ci" / "check-actions-storage.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules["check_actions_storage"] = module
SPEC.loader.exec_module(module)


def tree(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class ActionsStorageTest(unittest.TestCase):
    def test_clean_tree_passes(self):
        root = tree({".github/workflows/ci.yml": "jobs:\n  a:\n    steps:\n      - run: echo hi\n"})
        self.assertEqual(module.check(root), [])

    def test_each_kind_of_use_is_found(self):
        for line in (
            "uses: actions/upload-artifact@abc # v7",
            "uses: actions/download-artifact@abc",
            "uses: actions/cache@abc",
            "uses: actions/cache/restore@abc",
            "cache-to: type=gha,mode=max",
        ):
            root = tree({".github/workflows/ci.yml": f"jobs:\n  a:\n    steps:\n      - {line}\n"})
            self.assertEqual(len(module.check(root)), 1, line)

    def test_a_comment_is_not_a_use(self):
        root = tree({".github/workflows/ci.yml": "# actions/upload-artifact@abc is gone\njobs: {}\n"})
        self.assertEqual(module.check(root), [])

    def test_a_script_that_downloads_a_run_artifact_is_found_across_lines(self):
        root = tree({"scripts/qa/x.py": 'cmd = [\n    "gh",\n    "run",\n    "download",\n]\n'})
        self.assertEqual(len(module.check(root)), 1)

    def test_an_allowlisted_use_passes_and_a_stale_entry_fails(self):
        files = {
            ".github/workflows/native.yml": "jobs:\n  a:\n    steps:\n      - uses: actions/upload-artifact@abc\n",
            "scripts/ci/actions-storage.allow": "# why\n.github/workflows/native.yml upload-artifact\n",
        }
        self.assertEqual(module.check(tree(files)), [])
        files[".github/workflows/native.yml"] = "jobs: {}\n"
        problems = module.check(tree(files))
        self.assertEqual(len(problems), 1)
        self.assertIn("stale entry", problems[0])

    def test_the_repository_itself_is_clean(self):
        self.assertEqual(module.check(ROOT), [])


if __name__ == "__main__":
    unittest.main()
