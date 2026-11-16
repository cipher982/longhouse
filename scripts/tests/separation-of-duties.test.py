#!/usr/bin/env python3
"""scripts/ci/separation_of_duties.py: one commit never changes the verifier and the product it judges."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("separation_of_duties", ROOT / "scripts/ci/separation_of_duties.py")
sod = importlib.util.module_from_spec(spec)
sys.modules["separation_of_duties"] = sod
spec.loader.exec_module(sod)

ORACLE = "server/zerg/qa/terminal_truth.py"
CANARY = "scripts/qa/provider-control-e2e-canary.py"
SUBJECT = "server/zerg/services/session_runtime.py"
ENGINE = "engine/src/main.rs"
GUARD = "scripts/ci/separation_of_duties.py"
ALLOW = "scripts/ci/verifier-boundary.allow"


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.write({ORACLE: "o0\n", SUBJECT: "s0\n", ENGINE: "e0\n", GUARD: "g0\n", "docs/a.md": "d0\n", ALLOW: "x\n"})
        self.commit("base")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, text=True, capture_output=True, check=True).stdout.strip()

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


class SeparationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(Path(self.tmp.name))

    def verdict(self, commit: str):
        return sod.violation(commit, self.repo.path)

    def test_classes(self) -> None:
        self.assertEqual(sod.classify(ORACLE), "verifier")
        self.assertEqual(sod.classify(CANARY), "verifier")
        self.assertEqual(sod.classify("server/zerg/provider_contract/x.py"), "verifier")
        self.assertEqual(sod.classify(SUBJECT), "subject")
        self.assertEqual(sod.classify(ENGINE), "subject")
        self.assertEqual(sod.classify("schemas/managed_providers.yml"), "subject")
        self.assertEqual(sod.classify(GUARD), "guard")
        self.assertIsNone(sod.classify("web/src/a.ts"))
        self.assertIsNone(sod.classify(ALLOW))
        for derived in sod.DERIVED_FILES:
            self.assertIsNone(sod.classify(derived))

    def test_a_regenerated_manifest_rides_with_a_verifier_edit_but_not_a_subject_edit_in_disguise(self) -> None:
        manifest = "server/zerg/config/managed_provider_contracts.json"
        self.repo.write({ORACLE: "o1\n", manifest: "{}\n"})
        self.assertIsNone(self.verdict(self.repo.commit("oracle and its regenerated digest")))
        self.repo.write({ORACLE: "o2\n", manifest: "{1}\n", SUBJECT: "s1\n"})
        self.assertIsNotNone(self.verdict(self.repo.commit("oracle, manifest and a subject edit")))

    def test_single_class_and_unclassified_pass(self) -> None:
        self.repo.write({ORACLE: "o1\n", CANARY: "c\n", "docs/a.md": "d1\n", ALLOW: "y\n"})
        self.assertIsNone(self.verdict(self.repo.commit("verifier only")))
        self.repo.write({SUBJECT: "s1\n", ENGINE: "e1\n", "web/x.ts": "w\n"})
        self.assertIsNone(self.verdict(self.repo.commit("subject only")))

    def test_verifier_plus_subject_refused(self) -> None:
        self.repo.write({ORACLE: "o1\n", SUBJECT: "s1\n"})
        found = self.verdict(self.repo.commit("both"))
        self.assertEqual(set(found), {"verifier", "subject"})

    def test_verifier_plus_engine_and_guard_pairs_refused(self) -> None:
        self.repo.write({ORACLE: "o1\n", ENGINE: "e1\n"})
        self.assertIsNotNone(self.verdict(self.repo.commit("engine")))
        self.repo.write({GUARD: "g1\n", ORACLE: "o2\n"})
        self.assertIsNotNone(self.verdict(self.repo.commit("guard and verifier")))
        self.repo.write({GUARD: "g2\n", SUBJECT: "s2\n"})
        self.assertIsNotNone(self.verdict(self.repo.commit("guard and subject")))

    def test_deleting_a_verifier_file_counts(self) -> None:
        (self.repo.path / ORACLE).unlink()
        self.repo.write({SUBJECT: "s1\n"})
        self.assertIsNotNone(self.verdict(self.repo.commit("delete oracle and edit subject")))

    def test_exact_revert_of_a_mixed_commit_is_exempt(self) -> None:
        self.repo.write({ORACLE: "o1\n", SUBJECT: "s1\n"})
        mixed = self.repo.commit("both")
        self.repo.git("revert", "--no-edit", mixed)
        self.assertIsNone(self.verdict(self.repo.git("rev-parse", "HEAD")))

    def test_revert_message_with_other_content_is_not_exempt(self) -> None:
        self.repo.write({ORACLE: "o1\n", SUBJECT: "s1\n"})
        mixed = self.repo.commit("both")
        self.repo.write({ORACLE: "weakened\n", SUBJECT: "s9\n"})
        fake = self.repo.commit(f"Revert it\n\nThis reverts commit {mixed}.")
        self.assertIsNotNone(self.verdict(fake))

    def test_check_walks_the_range_and_skips_merges(self) -> None:
        base = self.repo.git("rev-parse", "HEAD")
        self.repo.write({ORACLE: "o1\n"})
        self.repo.commit("verifier")
        self.repo.write({SUBJECT: "s1\n"})
        self.repo.commit("subject")
        self.assertEqual(sod.check(base, "HEAD", self.repo.path), 0)
        self.repo.write({ORACLE: "o2\n", ENGINE: "e2\n"})
        self.repo.commit("mixed")
        self.assertEqual(sod.check(base, "HEAD", self.repo.path), 1)


if __name__ == "__main__":
    unittest.main()
