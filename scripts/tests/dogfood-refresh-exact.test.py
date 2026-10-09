#!/usr/bin/env python3
"""make dogfood-refresh builds one exact commit in a disposable checkout and removes it after.

The real dogfood-runtime.sh runs against a fixture clone whose committed copy of the script (the one
the disposable checkout runs) is a stub that records where and what it was asked to build, so nothing
here builds or installs anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

INNER_STUB = """#!/usr/bin/env bash
set -euo pipefail
{
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
  echo "head=$(git -C "$here" rev-parse HEAD)"
  echo "pwd=$(pwd -P)"
  echo "here=$here"
  echo "owner=${LONGHOUSE_CARGO_TARGET_OWNER:-}"
  echo "checkout=${LONGHOUSE_DOGFOOD_CHECKOUT:-}"
  echo "args=$*"
} >> "$FIXTURE_ROOT/inner"
[[ "${FIXTURE_INNER_FAIL:-0}" != 1 ]]
"""


class DogfoodRefreshExactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="longhouse-dogfood-exact-")
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "primary"
        self.lanes = self.root / "lanes"
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        script = self.repo / "scripts" / "dev" / "dogfood-runtime.sh"
        script.parent.mkdir(parents=True)
        script.write_text(INNER_STUB)
        script.chmod(0o755)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.first = self.git("rev-parse", "HEAD")
        (self.repo / "later.txt").write_text("x")
        self.git("add", "later.txt")
        self.git("commit", "-q", "-m", "second")
        # The outer run is the real script, in the working tree only (uncommitted, like an agent's edit).
        shutil.copyfile(ROOT / "scripts" / "dev" / "dogfood-runtime.sh", script)
        lib = self.repo / "scripts" / "lib"
        lib.mkdir(parents=True)
        for name in ("exact-checkout.sh", "heavy-build-lock.sh"):
            shutil.copyfile(ROOT / "scripts" / "lib" / name, lib / name)
        self.home = self.root / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        # The inner run installs longhouse-server from the exact checkout; the
        # stub stands in for that install and reports the requested commit.
        self.write_server_cli(self.first)

    def write_server_cli(self, sha: str) -> None:
        cli = self.home / ".local" / "bin" / "longhouse-server"
        cli.write_text(f"#!/usr/bin/env bash\necho longhouse 0.1.0-dev+{sha[:8]}\n")
        cli.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def refresh(self, *args, **env) -> subprocess.CompletedProcess:
        environment = {k: v for k, v in os.environ.items() if k not in ("LONGHOUSE_DOGFOOD_CHECKOUT", "LONGHOUSE_CARGO_TARGET_OWNER")}
        environment.update(FIXTURE_ROOT=str(self.root), HOME=str(self.home), LONGHOUSE_EXACT_CHECKOUT_PARENT=str(self.lanes),
                           LONGHOUSE_HEAVY_BUILD_LOCK=str(self.root / "heavy.lock"), **env)
        return subprocess.run(["bash", str(self.repo / "scripts" / "dev" / "dogfood-runtime.sh"), "refresh", *args],
                              cwd=self.repo, env=environment, capture_output=True, text=True, timeout=60)

    def inner(self) -> dict:
        lines = (self.root / "inner").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines)

    def assert_cleaned_up(self):
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(list(self.lanes.iterdir()) if self.lanes.exists() else [], [])

    def test_it_builds_the_requested_commit_in_a_disposable_checkout_with_the_primary_target(self):
        result = self.refresh("--sha", self.first, "--skip-engine", "--no-menubar")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        inner = self.inner()
        self.assertEqual(inner["head"], self.first)  # exactly that commit, not the checkout's HEAD or working tree
        self.assertEqual(Path(inner["here"]).parent, self.lanes)
        self.assertEqual(inner["pwd"], inner["here"])
        self.assertEqual(inner["owner"], str(self.repo))  # the primary checkout's Cargo target
        self.assertEqual(inner["checkout"], "1")
        self.assertEqual(inner["args"], "refresh --skip-engine --no-menubar")  # --sha is not forwarded
        self.assert_cleaned_up()

    def test_a_failed_refresh_still_removes_its_checkout(self):
        result = self.refresh("--sha", self.first, "--skip-engine", FIXTURE_INNER_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_cleaned_up()

    def test_the_installed_binary_must_report_the_requested_commit(self):
        binary = self.home / ".local" / "bin" / "longhouse"
        binary.write_text(f"#!/usr/bin/env bash\necho 0.1.0-dev+{self.first[:8]}\n")
        binary.chmod(0o755)
        self.assertEqual(self.refresh("--sha", self.first).returncode, 0)
        binary.write_text("#!/usr/bin/env bash\necho 0.1.0-dev+deadbeef\n")
        result = self.refresh("--sha", self.first)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not " + self.first[:8], result.stderr)
        self.assert_cleaned_up()

    def test_here_builds_this_working_tree_in_place(self):
        # --here is the old path: no disposable checkout. It needs a real runtime, so only its routing is checked.
        result = self.refresh("--here", "--skip-engine", "--url", "", CLAUDE_CONFIG_DIR=str(self.root / "claude"))
        self.assertFalse((self.root / "inner").exists())
        self.assertFalse(self.lanes.exists())
        self.assertIn("No canonical machine state", result.stderr + result.stdout)

    def test_longhouse_server_must_report_the_requested_commit(self):
        self.write_server_cli("deadbeefcafe")
        result = self.refresh("--sha", self.first, "--skip-engine", "--no-menubar")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("longhouse-server reports", result.stderr)
        self.assertIn("not " + self.first[:8], result.stderr)
        self.assert_cleaned_up()


if __name__ == "__main__":
    unittest.main()
