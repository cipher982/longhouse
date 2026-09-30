#!/usr/bin/env python3
"""The verifier/subject import boundary, its one-way allowlist, and the verifier digest."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "verifier_boundary.py"

spec = importlib.util.spec_from_file_location("verifier_boundary", SCRIPT)
vb = importlib.util.module_from_spec(spec)
sys.modules["verifier_boundary"] = vb
spec.loader.exec_module(vb)

QA = "server/zerg/qa/"
SVC = "server/zerg/services/"
CANARY = "scripts/qa/provider-control-e2e-canary.py"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# The control-plane provider_factory/verifier.py tests carry this same vector and the same path table.
GOLDEN_FILES = {
    "server/zerg/qa/alpha.py": sha(b"alpha\n"),
    "server/zerg/qa/provider_adapters/beta.py": sha(b"beta\n"),
    "scripts/qa/provider-control-e2e-canary.py": sha(b"canary\n"),
}
GOLDEN_DIGEST = "sha256:3d83f35526ce9fa48b5fa64b10088096615b0d8e8e4f460fc8d32c9c1f419dea"
GOLDEN_RULE = {
    "server/zerg/qa/alpha.py": True,
    "server/zerg/qa/provider_adapters/beta.py": True,
    "scripts/qa/provider-control-e2e-canary.py": True,
    "scripts/qa/other.py": False,
    "server/zerg/qa": False,
    "server/zerg/qa_helpers/x.py": False,
    "server/zerg/services/provider_capability_proof.py": False,
    "schemas/managed_providers.yml": False,
    "engine/src/main.rs": False,
}


def edges_of(sources: dict[str, str]) -> set[tuple[str, str]]:
    return vb.import_edges(sources)


class Sides(unittest.TestCase):
    def test_path_rule_table(self):
        for path, expected in GOLDEN_RULE.items():
            self.assertEqual(vb.is_verifier_path(path), expected, path)

    def test_sides(self):
        self.assertEqual(vb.side(QA + "x.py"), "verifier")
        self.assertEqual(vb.side(CANARY), "verifier")
        self.assertEqual(vb.side("server/zerg/provider_contract/proof.py"), "neutral")
        self.assertEqual(vb.side(SVC + "x.py"), "subject")


class ImportGraph(unittest.TestCase):
    def test_resolution(self):
        sources = {
            "server/zerg/__init__.py": "",
            "server/zerg/services/__init__.py": "",
            "server/zerg/services/proof.py": "",
            "server/zerg/services/paths.py": "",
            "server/zerg/services/shipper/__init__.py": "",
            "server/zerg/services/shipper/hooks.py": "",
            "server/zerg/config/__init__.py": "settings = 1\n",
            "server/zerg/qa/__init__.py": "",
            "server/zerg/qa/sibling.py": "",
            "server/zerg/qa/oracle.py": (
                "import json\n"
                "import zerg.services.proof\n"  # absolute module
                "from zerg.services import paths\n"  # from-import of a submodule
                "from zerg.config import settings\n"  # from-import of an attribute of a package
                "from zerg.services.shipper.hooks import Thing\n"  # attribute of a module
                "from . import sibling\n"  # relative, sibling module
                "from .sibling import thing\n"
                "from ..services import proof as p2\n"  # relative, up one
                "import requests\nfrom os import path\n"  # not zerg
                "def late():\n    from zerg.services.shipper import hooks\n"  # function-local
            ),
            "server/zerg/qa/dynamic.py": (
                "import importlib\n"
                "from importlib import import_module\n"
                'importlib.import_module("zerg.services.paths")\n'
                'import_module("zerg.services.proof")\n'
                "import_module(some_variable)\n"  # not a literal: invisible
                'import_module("json")\n'
            ),
            "server/zerg/qa/star.py": "from zerg.services.proof import *\n",
        }
        edges = edges_of(sources)
        got = {(a.removeprefix("server/zerg/"), b.removeprefix("server/zerg/")) for a, b in edges}
        self.assertEqual(
            got,
            {
                ("qa/oracle.py", "services/proof.py"),
                ("qa/oracle.py", "services/paths.py"),
                ("qa/oracle.py", "config/__init__.py"),
                ("qa/oracle.py", "services/shipper/hooks.py"),
                ("qa/oracle.py", "qa/sibling.py"),
                ("qa/dynamic.py", "services/paths.py"),
                ("qa/dynamic.py", "services/proof.py"),
                ("qa/star.py", "services/proof.py"),
            },
        )

    def test_imports_inside_code_strings_and_aliased_loaders_count(self):
        sources = {
            "server/zerg/services/__init__.py": "",
            "server/zerg/services/proof.py": "",
            "server/zerg/services/paths.py": "",
            "server/zerg/services/other.py": "",
            "server/zerg/services/late.py": "",
            "server/zerg/qa/embedded.py": (
                "import textwrap\n"
                "code = textwrap.dedent(\n"
                '    f"""\n'
                "    from pathlib import Path\n"
                "    from zerg.services import paths\n"
                "    print({str(1)!r})\n"
                '    """\n'
                ")\n"
                'script = "import zerg.services.proof\\nprint(1)"\n'
                'prose = "you can write from zerg.services import other in a script"\n'
                'quoted = "    from zerg.not valid python"\n'
                'names = f"""\n'
                "    from zerg.services import {symbol}\n"
                "    import zerg.{module}\n"
                "    from zerg.services.other import (\n"
                "        thing,\n"
                "    )\n"
                "    from zerg.services.late import (Named,\n"
                "        {more})\n"
                '    """\n'
            ),
            "server/zerg/qa/oneliner.py": (
                'ARGS = ["python", "-c", "import os; from zerg.services.late import x; print(x)"]\n'
                'OTHER = "import zerg.services.other as o;print(o)"\n'
                'PROSE = "then import zerg is fine, and so is a; from ordinary import thing"\n'
            ),
            "server/zerg/qa/aliased.py": (
                "import builtins\n"
                "from importlib import import_module as load\n"
                'load("zerg.services.other")\n'
                'builtins.__import__("zerg.services.paths")\n'
                'unrelated("zerg.services.proof")\n'
            ),
        }
        got = {(a.removeprefix("server/zerg/"), b.removeprefix("server/zerg/")) for a, b in edges_of(sources)}
        self.assertEqual(
            got,
            {
                ("qa/embedded.py", "services/paths.py"),
                ("qa/embedded.py", "services/proof.py"),
                ("qa/embedded.py", "services/__init__.py"),  # `from zerg.services import {symbol}`
                ("qa/embedded.py", "services/other.py"),  # a parenthesized list
                ("qa/embedded.py", "services/late.py"),  # names on the opener line
                ("qa/oneliner.py", "services/late.py"),
                ("qa/oneliner.py", "services/other.py"),
                ("qa/aliased.py", "services/other.py"),
                ("qa/aliased.py", "services/paths.py"),
            },
        )

    def test_package_init_relative_import_is_inside_the_package(self):
        sources = {
            "server/zerg/qa/__init__.py": "from .helper import x\n",
            "server/zerg/qa/helper.py": "",
        }
        self.assertEqual(edges_of(sources), {("server/zerg/qa/__init__.py", "server/zerg/qa/helper.py")})

    def test_canary_script_imports_count(self):
        sources = {CANARY: "from zerg.services import proof\n", SVC + "proof.py": ""}
        self.assertEqual(edges_of(sources), {(CANARY, SVC + "proof.py")})

    def test_read_sources_skips_caches_and_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (
                "server/zerg/a.py",
                "server/zerg/__pycache__/a.cpython-312.py",
                "server/zerg/.hidden/b.py",
                CANARY,
            ):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("", encoding="utf-8")
            self.assertEqual(sorted(vb.read_sources(root)), [CANARY, "server/zerg/a.py"])


class Crossing(unittest.TestCase):
    def test_directions(self):
        v, s, n = QA + "o.py", SVC + "s.py", "server/zerg/provider_contract/p.py"
        self.assertEqual(
            vb.crossing_edges([(v, QA + "o2.py"), (s, SVC + "s2.py"), (n, "server/zerg/provider_contract/q.py")]), set()
        )
        self.assertEqual(vb.crossing_edges([(v, n), (s, n)]), set(), "anyone may import the neutral package")
        for edge in [(v, s), (s, v), (n, s), (n, v), (CANARY, s)]:
            self.assertEqual(vb.crossing_edges([edge]), {edge}, edge)
        self.assertEqual(vb.direction((v, s)), "verifier->subject")
        self.assertEqual(vb.direction((s, v)), "subject->verifier")


class Allowlist(unittest.TestCase):
    E1 = (QA + "a.py", SVC + "x.py")
    E2 = (QA + "b.py", SVC + "y.py")

    def allow(self, *edges):
        return {edge: "reason" for edge in edges}

    def test_parse(self):
        text = f"# comment\n\n{self.E1[0]} -> {self.E1[1]}  # why\n"
        entries, problems = vb.parse_allowlist(text)
        self.assertEqual((entries, problems), ({self.E1: "why"}, []))

    def test_parse_rejects_missing_reason_duplicates_and_garbage(self):
        line = f"{self.E1[0]} -> {self.E1[1]}"
        for text, needle in [
            (line + "\n", "no reason"),
            (line + "  #   \n", "no reason"),
            (f"{line}  # a\n{line}  # b\n", "duplicate"),
            ("not an entry  # x\n", "expected"),
            (f"{self.E1[0]} ->  # x\n", "expected"),
        ]:
            _, problems = vb.parse_allowlist(text)
            self.assertTrue(any(needle in problem for problem in problems), (text, problems))

    def test_new_edge_fails(self):
        problems = vb.check_edges({self.E1, self.E2}, self.allow(self.E1))
        self.assertEqual(len(problems), 1)
        self.assertIn("new verifier->subject import", problems[0])
        self.assertIn(self.E2[0], problems[0])

    def test_stale_entry_fails(self):
        problems = vb.check_edges({self.E1}, self.allow(self.E1, self.E2))
        self.assertEqual(len(problems), 1)
        self.assertIn("stale allowlist entry", problems[0])
        self.assertIn(self.E2[1], problems[0])

    def test_non_crossing_import_needs_no_entry(self):
        self.assertEqual(vb.check_edges({(QA + "a.py", QA + "b.py"), (SVC + "a.py", SVC + "b.py")}, {}), [])

    def test_allowlist_cannot_grow_past_its_base(self):
        both = {self.E1, self.E2}
        # A new import plus a matching new entry passes the exact check but not the ratchet.
        problems = vb.check_edges(both, self.allow(self.E1, self.E2), base_allowed=self.allow(self.E1))
        self.assertEqual(len(problems), 1)
        self.assertIn("only shrinks", problems[0])
        self.assertEqual(vb.check_edges({self.E1}, self.allow(self.E1), base_allowed=self.allow(self.E1, self.E2)), [])
        self.assertEqual(
            vb.check_edges(both, self.allow(self.E1, self.E2), base_allowed=self.allow(self.E1, self.E2)), []
        )


class Tree:
    """A throwaway source tree with the allowlist beside it, optionally a git repo."""

    def __init__(self, directory: str):
        self.root = Path(directory)

    def write(self, name: str, body: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--root", str(self.root)], capture_output=True, text=True, check=False
        )

    def git(self, *args: str, date: str | None = None) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": date or "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": date or "2026-01-01T00:00:00Z",
        }
        done = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True, env=env)
        return done.stdout.strip()

    def init_git(self) -> None:
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def commit(self, message: str, date: str | None = None) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, date=date)
        return self.git("rev-parse", "HEAD")


def fixture(tmp: str) -> Tree:
    tree = Tree(tmp)
    tree.write("server/zerg/services/proof.py", "VALUE = 1\n")
    tree.write("server/zerg/services/paths.py", "VALUE = 2\n")
    tree.write("server/zerg/qa/oracle.py", "from zerg.services import proof\n")
    tree.write("scripts/ci/verifier-boundary.allow", f"{QA}oracle.py -> {SVC}proof.py  # fixture reason\n")
    return tree


class Cli(unittest.TestCase):
    def test_clean_tree_passes_and_prints_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = fixture(tmp).run("check")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("verifier_digest sha256:", done.stdout)
            self.assertEqual(done.stdout, fixture_output(tmp), "the default command is check")

    def test_a_new_import_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = fixture(tmp)
            tree.write("server/zerg/qa/oracle.py", "from zerg.services import proof, paths\n")
            done = tree.run("check")
            self.assertEqual(done.returncode, 1)
            self.assertIn(f"{QA}oracle.py -> {SVC}paths.py", done.stderr)

    def test_a_new_verifier_file_importing_the_subject_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = fixture(tmp)
            tree.write("server/zerg/qa/fresh.py", "import zerg.services.paths\n")
            self.assertEqual(tree.run("check").returncode, 1)

    def test_a_subject_import_of_the_verifier_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = fixture(tmp)
            tree.write("server/zerg/services/paths.py", "from zerg.qa import oracle\n")
            done = tree.run("check")
            self.assertEqual(done.returncode, 1)
            self.assertIn("subject->verifier", done.stderr)

    def test_deleting_an_import_without_its_entry_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = fixture(tmp)
            tree.write("server/zerg/qa/oracle.py", "VALUE = 3\n")
            done = tree.run("check")
            self.assertEqual(done.returncode, 1)
            self.assertIn("stale allowlist entry", done.stderr)
            tree.write("scripts/ci/verifier-boundary.allow", "# nothing left\n")
            self.assertEqual(tree.run("check").returncode, 0)

    def test_base_comparison_makes_the_allowlist_one_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = fixture(tmp)
            tree.init_git()
            base = tree.commit("base")
            # New import and a matching new entry: exact check passes, ratchet does not.
            tree.write("server/zerg/qa/oracle.py", "from zerg.services import proof, paths\n")
            tree.write(
                "scripts/ci/verifier-boundary.allow",
                f"{QA}oracle.py -> {SVC}proof.py  # r\n{QA}oracle.py -> {SVC}paths.py  # added\n",
            )
            self.assertEqual(tree.run("check").returncode, 0)
            done = tree.run("check", "--base", base)
            self.assertEqual(done.returncode, 1)
            self.assertIn("only shrinks", done.stderr)
            # Removing the import and its entry passes against the same base.
            tree.write("server/zerg/qa/oracle.py", "VALUE = 3\n")
            tree.write("scripts/ci/verifier-boundary.allow", "# empty\n")
            self.assertEqual(tree.run("check", "--base", base).returncode, 0)

    def test_rev_checks_the_committed_tree_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = fixture(tmp)
            tree.init_git()
            tree.commit("clean")
            tree.write("server/zerg/qa/oracle.py", "from zerg.services import proof, paths\n")  # uncommitted
            self.assertEqual(tree.run("check").returncode, 1)
            done = tree.run("check", "--rev", "HEAD")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("verifier_digest sha256:", done.stdout)
            tree.commit("adds a crossing import")
            tree.write("server/zerg/qa/oracle.py", "from zerg.services import proof\n")  # uncommitted revert
            self.assertEqual(tree.run("check").returncode, 0)
            done = tree.run("check", "--rev", "HEAD")
            self.assertEqual(done.returncode, 1)
            self.assertIn(f"{QA}oracle.py -> {SVC}paths.py", done.stderr)
            self.assertNotEqual(tree.run("check", "--rev", "0" * 40).returncode, 0)

    def test_base_without_an_allowlist_skips_the_comparison_and_a_bad_base_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = fixture(tmp)
            tree.init_git()
            (tree.root / "scripts/ci/verifier-boundary.allow").rename(tree.root / "allow.tmp")
            base = tree.commit("before the allowlist existed")
            (tree.root / "allow.tmp").rename(tree.root / "scripts/ci/verifier-boundary.allow")
            done = tree.run("check", "--base", base)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("skipping the only-shrinks comparison", done.stdout)
            self.assertNotEqual(tree.run("check", "--base", "0" * 40).returncode, 0)


def fixture_output(tmp: str) -> str:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", tmp], capture_output=True, text=True, check=False
    ).stdout


class Wiring(unittest.TestCase):
    """The growth half of the ratchet needs git history, which make validate's guest lacks: the host
    pre-push paths and contract-first CI must run it."""

    def source(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_the_host_push_paths_and_ci_run_the_base_comparison(self):
        for name in ("scripts/ops/check-push-readiness.sh", "scripts/ops/ship.sh"):
            invocation = re.search(r"^\s*python3 .*verifier_boundary\.py.*$", self.source(name), re.MULTILINE)
            self.assertIsNotNone(invocation, name)
            self.assertIn(" check ", invocation.group(0), name)
            self.assertIn("--rev ", invocation.group(0), name)
        self.assertIn("--base", self.source("scripts/ops/ship.sh"))
        self.assertIn("--base", self.source("scripts/ops/check-push-readiness.sh"))
        workflow = self.source(".github/workflows/contract-first-ci.yml")
        self.assertIn("verifier_boundary.py check --base FETCH_HEAD", workflow)

    def test_make_validate_runs_the_exact_check_and_these_tests(self):
        makefile = self.source("Makefile")
        self.assertIn("validate-verifier-boundary \\\n", makefile.split("VALIDATE_MEMBERS :=")[1].split("\n\n")[0])
        target = makefile.split("validate-verifier-boundary: ##")[1].split("\n\n")[0]
        self.assertIn("scripts/tests/verifier-boundary.test.py", target)
        self.assertIn("scripts/ci/verifier_boundary.py check", target)


class Digest(unittest.TestCase):
    def test_golden_vector(self):
        self.assertEqual(vb.digest_files(GOLDEN_FILES), GOLDEN_DIGEST)

    def test_order_independent_and_content_sensitive(self):
        reordered = dict(reversed(list(GOLDEN_FILES.items())))
        self.assertEqual(vb.digest_files(reordered), GOLDEN_DIGEST)
        changed = {**GOLDEN_FILES, "server/zerg/qa/alpha.py": sha(b"alpha changed\n")}
        self.assertNotEqual(vb.digest_files(changed), GOLDEN_DIGEST)
        renamed = {"server/zerg/qa/gamma.py": GOLDEN_FILES["server/zerg/qa/alpha.py"]}
        self.assertNotEqual(vb.digest_files({**GOLDEN_FILES, **renamed}), GOLDEN_DIGEST)

    def test_working_tree_digest_follows_the_path_rule_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Tree(tmp)
            tree.write("server/zerg/qa/alpha.py", "alpha\n")
            tree.write("server/zerg/qa/provider_adapters/beta.py", "beta\n")
            tree.write(CANARY, "canary\n")
            tree.write("server/zerg/services/subject.py", "one\n")
            tree.write("server/zerg/qa/__pycache__/alpha.cpython-312.pyc", "junk")
            tree.write("server/zerg/qa/.DS_Store", "junk")
            root = Path(tmp)
            self.assertEqual(vb.verifier_digest(root), GOLDEN_DIGEST)
            tree.write("server/zerg/services/subject.py", "two\n")
            self.assertEqual(vb.verifier_digest(root), GOLDEN_DIGEST, "subject files are not in the digest")
            tree.write("server/zerg/qa/alpha.py", "alpha edited\n")
            self.assertNotEqual(vb.verifier_digest(root), GOLDEN_DIGEST)
            tree.write("server/zerg/qa/alpha.py", "alpha\n")
            tree.write("server/zerg/qa/gamma.py", "new file\n")
            self.assertNotEqual(vb.verifier_digest(root), GOLDEN_DIGEST, "a new qa file changes the digest")

    def test_explicit_path_set_digests_exactly_those_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Tree(tmp)
            tree.write("server/zerg/qa/alpha.py", "alpha\n")
            tree.write("server/zerg/qa/unpinned.py", "not pinned\n")
            only_alpha = vb.digest_files({"server/zerg/qa/alpha.py": sha(b"alpha\n")})
            self.assertEqual(vb.verifier_digest(Path(tmp), ["server/zerg/qa/alpha.py"]), only_alpha)
            done = subprocess.run(
                [sys.executable, str(SCRIPT), "digest", "--root", tmp], capture_output=True, text=True, check=True
            )
            self.assertIn(vb.verifier_digest(Path(tmp)), done.stdout)
            self.assertIn("2 files", done.stdout)


class Replay(unittest.TestCase):
    def test_counts_digest_changes_per_path_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Tree(tmp)
            tree.init_git()
            tree.write("server/zerg/qa/a.py", "a1\n")
            tree.write("server/zerg/services/s.py", "s1\n")
            tree.write("engine/x.rs", "x1\n")
            tree.commit("c1 creates everything", date="2026-01-01T00:00:00Z")  # root commit: changes every set
            tree.write("server/zerg/services/s.py", "s2\n")
            tree.commit("c2 subject only", date="2026-01-02T00:00:00Z")
            tree.write("engine/x.rs", "x2\n")
            tree.commit("c3 unrelated pinned file", date="2026-01-03T00:00:00Z")
            tree.write("server/zerg/qa/a.py", "a2\n")
            tree.commit("c4 verifier", date="2026-01-04T00:00:00Z")
            tree.write("server/zerg/qa/unpinned.py", "u1\n")
            tree.commit("c5 qa file outside the pinned set", date="2026-01-05T00:00:00Z")
            tree.write("README.md", "docs\n")
            tree.commit("c6 docs", date="2026-01-06T00:00:00Z")
            sets = {
                "rule": None,
                "pinned": ["server/zerg/qa/a.py"],
                "protected": ["server/zerg/qa/a.py", "server/zerg/services/s.py", "engine/x.rs"],
            }
            total, changed = vb.replay(tree.root, "HEAD", "2026-01-02T00:00:00Z", sets)
            self.assertEqual(total, 5, "the window drops c1")
            self.assertEqual(
                {name: len(commits) for name, commits in changed.items()}, {"rule": 2, "pinned": 1, "protected": 3}
            )
            total, changed = vb.replay(tree.root, "HEAD", "2025-01-01T00:00:00Z", sets)
            self.assertEqual(total, 6)
            self.assertEqual(
                {name: len(commits) for name, commits in changed.items()}, {"rule": 3, "pinned": 2, "protected": 4}
            )

    def test_cli_prints_the_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Tree(tmp)
            tree.init_git()
            tree.write("server/zerg/qa/a.py", "a1\n")
            tree.write("server/zerg/services/s.py", "s1\n")
            tree.commit("c1", date="2026-01-01T00:00:00Z")
            tree.write("server/zerg/services/s.py", "s2\n")
            tree.commit("c2", date="2026-01-02T00:00:00Z")
            (tree.root / "pinned.txt").write_text("server/zerg/qa/a.py\n")
            (tree.root / "protected.txt").write_text("server/zerg/qa/a.py\nserver/zerg/services/s.py\n")
            done = tree.run(
                "replay",
                "--since",
                "2026-01-02T00:00:00Z",
                "--pinned-file",
                str(tree.root / "pinned.txt"),
                "--protected-file",
                str(tree.root / "protected.txt"),
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("pin changed, pinned verifier did not: 1", done.stdout)
            self.assertIn("pinned verifier changed, pin did not: 0", done.stdout)


if __name__ == "__main__":
    unittest.main()
