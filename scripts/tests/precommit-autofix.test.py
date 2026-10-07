#!/usr/bin/env python3
"""Self-staging pre-commit fixes (scripts/qa/precommit_autofix.py), without pre-commit itself.

The test process plays pre-commit: it runs the snapshot shim's mode with its own pid, then the fixer as
its child, so the fixer trusts that record exactly as it trusts pre-commit's. The full path (pre-commit's
stash, `git commit -o`, the installed shims) was proven in a scratch clone; see the module docstring.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa" / "precommit_autofix.py"
# A deterministic stand-in fixer: uppercases each file it is given; a file containing "BROKEN" is an error.
FIXER = """import sys
for name in sys.argv[1:]:
    text = open(name).read()
    if "BROKEN" in text:
        sys.exit(2)
    open(name, "w").write(text.upper())
"""
GENERATOR = """import pathlib
pathlib.Path("generated.txt").write_text(str(len(pathlib.Path("source.txt").read_text())) + "\\n")
"""


class AutofixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        (self.dir / "fixer.py").write_text(FIXER)
        (self.dir / "gen.py").write_text(GENERATOR)
        for name in ("a.txt", "b.txt", "source.txt", "generated.txt"):
            (self.dir / name).write_text("x\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True, text=True).stdout

    def autofix(self, *args: str, pid: int | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
        if pid is not None:
            env["LONGHOUSE_AUTOFIX_PID"] = str(pid)
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.dir, capture_output=True, text=True, env=env)

    def snapshot(self):
        self.assertEqual(self.autofix("snapshot", pid=os.getpid()).returncode, 0)

    def fix(self, *files: str, cmd: str = "fixer.py", watch: str | None = None) -> subprocess.CompletedProcess:
        args = ["fix", "--name", "upper", "--cmd", f"{sys.executable} {cmd}", "--ok-exit", "0", "--how", "fix it by hand"]
        if watch:
            args += ["--watch", watch]
        return self.autofix(*args, *files)

    def staged(self, name: str) -> str:
        return self.git("show", f":{name}")

    def test_the_post_commit_record_carries_the_pre_fix_blob(self):
        (self.dir / "a.txt").write_text("hello\n")
        self.git("add", "a.txt")
        self.snapshot()
        self.assertEqual(self.fix("a.txt").returncode, 0)
        state = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "longhouse-autofix"
        self.assertEqual((state / "staged").read_text(), f"a.txt\t{self.blob('hello')}\n")

    def test_a_fully_staged_file_is_fixed_and_restaged_so_the_commit_carries_the_fix(self):
        (self.dir / "a.txt").write_text("hello\n")
        self.git("add", "a.txt")
        self.snapshot()
        result = self.fix("a.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixed and re-staged a.txt", result.stdout)
        self.assertEqual(self.staged("a.txt"), "HELLO\n")
        self.assertEqual(self.git("status", "--short"), "M  a.txt\n")

    def test_a_file_with_unstaged_changes_is_left_alone_and_the_commit_fails(self):
        (self.dir / "a.txt").write_text("hello\n")
        self.git("add", "a.txt")
        (self.dir / "a.txt").write_text("hello\nmore, unstaged\n")
        self.snapshot()  # records a.txt as having unstaged changes
        # pre-commit would stash them here; the test stands in for that by restoring the staged content.
        (self.dir / "a.txt").write_text(self.staged("a.txt"))
        result = self.fix("a.txt")
        self.assertEqual(result.returncode, 1)
        self.assertIn("also has unstaged changes", result.stderr)
        self.assertIn("fix it by hand", result.stderr)
        self.assertEqual((self.dir / "a.txt").read_text(), "hello\n", "the fix was undone")
        self.assertEqual(self.staged("a.txt"), "hello\n")

    def test_only_the_safe_files_of_a_mixed_batch_are_staged(self):
        for name in ("a.txt", "b.txt"):
            (self.dir / name).write_text(f"{name}\n")
        self.git("add", "a.txt", "b.txt")
        (self.dir / "b.txt").write_text("b.txt\nunstaged\n")
        self.snapshot()
        (self.dir / "b.txt").write_text(self.staged("b.txt"))
        result = self.fix("a.txt", "b.txt")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.staged("a.txt"), "A.TXT\n")
        self.assertEqual(self.staged("b.txt"), "b.txt\n")

    def test_without_a_record_from_this_run_the_fix_stays_in_the_working_tree_and_fails(self):
        (self.dir / "a.txt").write_text("hello\n")
        self.git("add", "a.txt")
        self.assertEqual(self.autofix("snapshot", pid=1).returncode, 0)  # another process's record
        result = self.fix("a.txt")
        self.assertEqual(result.returncode, 1)
        self.assertIn("make install-push-gate", result.stderr)
        self.assertEqual((self.dir / "a.txt").read_text(), "HELLO\n")
        self.assertEqual(self.staged("a.txt"), "hello\n", "nothing was staged without the record")

    def test_a_fixer_error_fails_loudly_and_changes_nothing(self):
        (self.dir / "a.txt").write_text("hello\n")
        (self.dir / "b.txt").write_text("BROKEN\n")
        self.git("add", "a.txt", "b.txt")
        self.snapshot()
        result = self.fix("a.txt", "b.txt")
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed (exit 2)", result.stderr)
        self.assertEqual((self.dir / "a.txt").read_text(), "hello\n", "a fix made before the error was undone")
        self.assertEqual(self.staged("a.txt"), "hello\n")

    def test_nothing_to_fix_passes_silently(self):
        (self.dir / "a.txt").write_text("HELLO\n")
        self.git("add", "a.txt")
        self.snapshot()
        result = self.fix("a.txt")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_a_generated_file_is_regenerated_from_the_commit_and_staged(self):
        (self.dir / "source.txt").write_text("four\n")
        self.git("add", "source.txt")
        self.snapshot()
        result = self.fix(cmd="gen.py", watch="generated.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.staged("generated.txt"), "5\n")

    def test_post_commit_brings_the_index_up_to_a_fix_committed_from_a_temporary_index(self):
        # `git commit -o a.txt` commits a fix staged in its temporary index; the real index keeps the
        # unfixed content, which would show the fix reverted as a staged change.
        (self.dir / "a.txt").write_text("hello\n")
        self.git("add", "a.txt")
        (self.dir / "a.txt").write_text("HELLO\n")
        self.git("commit", "-q", "-o", "a.txt", "-m", "fixed")  # HEAD has the fix
        self.git("update-index", "--cacheinfo", f"100644,{self.blob('hello')},a.txt")  # the stale real index
        self.assertEqual(self.git("status", "--short"), "MM a.txt\n")
        state = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "longhouse-autofix"
        state.mkdir(exist_ok=True)
        (state / "staged").write_text(f"a.txt\t{self.blob('hello')}\n")
        (self.dir / "b.txt").write_text("someone's unrelated edit\n")
        self.git("add", "b.txt")
        self.assertEqual(self.autofix("post-commit").returncode, 0)
        self.assertEqual(self.git("status", "--short"), "M  b.txt\n", "a.txt settled; the unrelated staged edit untouched")
        self.assertFalse((state / "staged").exists())

    def blob(self, text: str) -> str:
        return subprocess.run(["git", "hash-object", "-w", "--stdin", "--path", "a.txt"], cwd=self.dir, input=text + "\n",
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_post_commit_settles_under_line_ending_filters(self):
        # With autocrlf, the blob `git add` writes is the cleaned (LF) content; the record must match it.
        self.git("config", "core.autocrlf", "true")
        (self.dir / "a.txt").write_bytes(b"hello\r\n")
        self.git("add", "a.txt")
        self.snapshot()
        self.assertEqual(self.fix("a.txt").returncode, 0)
        state = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "longhouse-autofix"
        self.assertEqual((state / "staged").read_text(), f"a.txt\t{self.blob('hello')}\n")
        # And post-commit settles the real index against it: commit the fix, then leave the real index holding
        # the pre-fix copy, as `git commit -o` does.
        self.git("commit", "-q", "-m", "fixed")
        self.git("update-index", "--cacheinfo", f"100644,{self.blob('hello')},a.txt")
        self.assertEqual(self.git("status", "--short"), "MM a.txt\n")
        self.assertEqual(self.autofix("post-commit").returncode, 0)
        self.assertEqual(self.git("status", "--short"), "")

    def test_a_generated_file_the_fix_created_is_settled_into_the_index_after_commit_o(self):
        (self.dir / "source.txt").write_text("four\n")
        self.git("add", "source.txt")
        self.git("rm", "-q", "--cached", "generated.txt")
        (self.dir / "generated.txt").unlink()
        self.git("commit", "-q", "-m", "no generated file yet")
        (self.dir / "source.txt").write_text("seven\n")
        self.git("add", "source.txt")
        self.snapshot()
        self.assertEqual(self.fix(cmd="gen.py", watch="generated.txt").returncode, 0)
        state = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "longhouse-autofix"
        self.assertEqual((state / "staged").read_text(), "generated.txt\tabsent\n")
        # `commit -o source.txt` committed both from its temporary index; the real index never saw the new file.
        self.git("commit", "-q", "-m", "with the generated file")
        self.git("rm", "-q", "--cached", "generated.txt")
        self.assertEqual(self.git("status", "--short"), "D  generated.txt\n?? generated.txt\n")
        (state / "staged").write_text("generated.txt\tabsent\n")
        self.assertEqual(self.autofix("post-commit").returncode, 0)
        self.assertEqual(self.git("status", "--short"), "", "no phantom staged deletion")

    def test_post_commit_never_touches_a_path_whose_working_tree_moved_on(self):
        (self.dir / "a.txt").write_text("edited after the commit\n")
        state = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "longhouse-autofix"
        state.mkdir(exist_ok=True)
        (state / "staged").write_text(f"a.txt\t{self.blob('hello')}\n")
        self.assertEqual(self.autofix("post-commit").returncode, 0)
        self.assertEqual(self.git("status", "--short"), " M a.txt\n")

    def test_post_commit_never_overwrites_a_different_edit_staged_to_the_fixed_path(self):
        (self.dir / "a.txt").write_text("staged by someone, then the working tree reverted\n")
        self.git("add", "a.txt")
        (self.dir / "a.txt").write_text("x\n")  # the working tree equals HEAD again
        state = Path(self.git("rev-parse", "--absolute-git-dir").strip()) / "longhouse-autofix"
        state.mkdir(exist_ok=True)
        (state / "staged").write_text(f"a.txt\t{self.blob('hello')}\n")  # the fix's pre-fix copy was something else
        self.assertEqual(self.autofix("post-commit").returncode, 0)
        self.assertEqual(self.git("status", "--short"), "MM a.txt\n", "the other staged edit survives")

    def test_the_shims_are_installed_once_per_clone_and_pass_pre_commits_pid(self):
        installer = (ROOT / "scripts" / "ops" / "install-push-gate.sh").read_text()
        self.assertIn("install_shim pre-commit.legacy", installer)
        self.assertIn("install_shim post-commit", installer)
        self.assertIn('LONGHOUSE_AUTOFIX_PID="\\$PPID"', installer)

    def test_the_config_never_autofixes_lint(self):
        config = (ROOT / ".pre-commit-config.yaml").read_text()
        for line in config.splitlines():
            if "precommit_autofix.py fix" in line:
                self.assertNotIn("--fix", line.split("--cmd", 1)[1].split("--how")[0])
        self.assertIn("require_serial: true", config)


if __name__ == "__main__":
    unittest.main()
