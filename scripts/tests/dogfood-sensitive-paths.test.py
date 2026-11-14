#!/usr/bin/env python3
"""Dogfood's fast lane must stay outside sensitive paths across every commit in a range."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
import dogfood_sensitive_paths as classifier  # noqa: E402


class FixtureRepo:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="longhouse-dogfood-sensitive-paths-")
        self.path = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "fixture")

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def commit(self, files: dict[str, str | None], message: str) -> str:
        for name, content in files.items():
            path = self.path / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        self.git("add", "--", *files)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def close(self) -> None:
        self.tmp.cleanup()


class DogfoodSensitivePathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FixtureRepo()
        self.addCleanup(self.repo.close)
        self.base = self.repo.commit({"README.md": "base\n"}, "base")

    def test_auth_and_migration_changes_never_use_fast_lane(self) -> None:
        docs = self.repo.commit({"README.md": "docs\n"}, "ordinary docs")
        self.assertFalse(classifier.has_sensitive_paths(self.repo.path, self.base, docs))

        auth = self.repo.commit({"server/zerg/auth/tokens.py": "token logic\n"}, "auth change")
        self.assertTrue(classifier.has_sensitive_paths(self.repo.path, docs, auth))

        reverted = self.repo.commit({"server/zerg/auth/tokens.py": None}, "revert auth change")
        self.assertTrue(classifier.has_sensitive_paths(self.repo.path, auth, reverted))
        self.assertTrue(classifier.has_sensitive_paths(self.repo.path, docs, reverted))

        migration = self.repo.commit({"server/zerg/db_migrations.py": "migration logic\n"}, "schema change")
        self.assertTrue(classifier.has_sensitive_paths(self.repo.path, reverted, migration))

    def test_every_sensitive_glob_still_matches_a_tracked_path(self) -> None:
        paths, patterns = classifier.load_patterns()
        tracked = classifier.git(ROOT, "ls-files").splitlines()
        unused = [path for path, pattern in zip(paths, patterns)
                  if not any(pattern.fullmatch(name) for name in tracked)]
        self.assertEqual(unused, [], "a path rename must not silently switch off a canary qualification")

    # --- live-preview paths: the commit whose terminal-truth status a promotion needs (not lane-sensitive) ---

    def test_the_newest_commit_touching_a_live_preview_path_is_found_and_none_is_lane_sensitive(self) -> None:
        first = self.repo.commit({"engine/src/pi_print.rs": "a\n"}, "pi print")
        docs = self.repo.commit({"README.md": "docs\n"}, "docs")
        second = self.repo.commit({"server/zerg/services/session_live_previews.py": "b\n"}, "previews")
        later = self.repo.commit({"README.md": "docs2\n"}, "docs again")
        self.assertFalse(classifier.has_sensitive_paths(self.repo.path, self.base, first))
        self.assertEqual(classifier.newest_live_preview_commit(self.repo.path, self.base, later), second)
        self.assertEqual(classifier.newest_live_preview_commit(self.repo.path, self.base, docs), first)
        self.assertIsNone(classifier.newest_live_preview_commit(self.repo.path, second, later))

    def test_a_live_preview_edit_that_is_reverted_still_counts(self) -> None:
        edit = self.repo.commit({"engine/src/codex_exec.rs": "x\n"}, "edit")
        revert = self.repo.commit({"engine/src/codex_exec.rs": None}, "revert")
        self.assertEqual(classifier.newest_live_preview_commit(self.repo.path, self.base, revert), revert)
        self.assertFalse(classifier.has_sensitive_paths(self.repo.path, edit, revert))

    def test_the_cli_prints_the_commit_or_nothing_and_exits_2_when_it_cannot_decide(self) -> None:
        touched = self.repo.commit({"engine/src/omp_helm_launcher.rs": "x\n"}, "launcher")
        script = ROOT / "scripts" / "ops" / "dogfood_sensitive_paths.py"
        run = lambda base, head: subprocess.run(  # noqa: E731
            [sys.executable, str(script), "--repo", str(self.repo.path), "live-preview", "--base", base, "--head", head],
            capture_output=True, text=True)
        hit = run(self.base, touched)
        self.assertEqual((hit.returncode, hit.stdout.strip()), (0, touched))
        miss = run(touched, touched)
        self.assertEqual((miss.returncode, miss.stdout), (0, ""))
        self.assertEqual(run("0" * 40, touched).returncode, 2)

    def test_the_live_preview_list_is_a_copy_of_the_workflow_push_filter(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "terminal-truth.yml").read_text()
        block = workflow.split("    paths:\n", 1)[1].split("  schedule:", 1)[0]
        pushed = re.findall(r'^\s+- "([^"]+)"$', block, re.M)
        self.assertEqual(classifier.load_live_preview_paths(), pushed)


if __name__ == "__main__":
    unittest.main()
