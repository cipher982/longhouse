#!/usr/bin/env python3
"""Dogfood's fast lane must stay outside sensitive paths across every commit in a range."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
