#!/usr/bin/env python3
"""The review gate: push rule, promotion rule, receipts, dispositions and the override."""

from __future__ import annotations

import contextlib
import http.server
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "ops" / "review_gate.py"
INSTALLER = ROOT / "scripts" / "ops" / "install-push-gate.sh"
POLICY = ROOT / "scripts" / "ops" / "review-policy.toml"

# No test launches a real review: automatic reviews are off unless a test points them at FAKE_HATCH.
os.environ["LONGHOUSE_AUTOREVIEW_HATCH"] = "off"

# Stands in for `hatch review`: appends the receipt review-hub would write for the range (or merge) it was
# given, in the repo's git common dir. FAKE_HATCH_PLAN (a file of states, one per line, consumed in order;
# default complete) lets a test script a partial first run; FAKE_HATCH_SLEEP delays it; every call's argv is
# appended to FAKE_HATCH_CALLS.
FAKE_HATCH = r'''#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys, time
args = sys.argv[1:]
assert args[0] == "review", args
with open(os.environ["FAKE_HATCH_CALLS"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
time.sleep(float(os.environ.get("FAKE_HATCH_SLEEP", "0")))
repo = args[args.index("-C") + 1]
spec = importlib.util.spec_from_file_location("review_gate", os.environ["FAKE_HATCH_GATE"])
gate = importlib.util.module_from_spec(spec); sys.modules["review_gate"] = gate; spec.loader.exec_module(gate)
state = "complete"
plan = os.environ.get("FAKE_HATCH_PLAN")
if plan and os.path.exists(plan):
    lines = open(plan).read().split()
    if lines:
        state = lines[0]
        open(plan, "w").write("\n".join(lines[1:]))
if "--merge" in args:
    sha = args[args.index("--merge") + 1]
    commits = [{"sha": sha, "patch_id": None, "subject": "", "merge": True}]
    base, head = sha + "^", sha
else:
    base, head = args[args.index("--base") + 1], args[args.index("--head") + 1]
    commits = []
    for c in gate.commits_in(repo, f"{base}..{head}"):
        if not c.merge:
            commits.append({"sha": c.sha, "patch_id": c.patch_id(repo), "subject": c.subject})
gate.append_event(repo, {"type": "review", "id": f"rv-fake-{time.time_ns()}", "at": gate._stamp(), "base": base,
                         "head": head, "commits": commits, "state": state, "state_reasons": [] if state == "complete" else ["budget"],
                         "findings": [], "verdict": "ready", "intent": {"no_intent": "--no-intent" in args}})
print(json.dumps({"review": "fake"}))
'''

spec = importlib.util.spec_from_file_location("review_gate", GATE)
gate = importlib.util.module_from_spec(spec)
sys.modules["review_gate"] = gate
spec.loader.exec_module(gate)

FIXTURE_POLICY = """
[exempt]
paths = ["**/*.md", "docs/**", "server/tests_lite/**", "**/test_*.py"]

[release_bump]
subject = '^Bump version to \\d+\\.\\d+\\.\\d+$'
paths = ["server/pyproject.toml", ".bumpversion.toml"]

[[repos.fixture.blocking]]
area = "auth"
paths = ["server/zerg/auth/**"]

[[repos.fixture.blocking]]
area = "engine state"
paths = ["engine/src/state/**", "engine/src/*binding*.rs"]
"""


class Repo:
    """A throwaway git repo with a fixture policy and helpers to commit and record receipts."""

    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("remote", "add", "origin", "git@github.com:example/fixture.git")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, subject: str, files: dict[str, str]) -> str:
        for name, body in files.items():
            path = self.dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            self.git("add", name)
        self.git("commit", "-q", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def commits(self, base: str, head: str = "HEAD") -> list[dict]:
        out = []
        for sha in self.git("rev-list", "--reverse", "--no-merges", f"{base}..{head}").split():  # as review-hub lists them
            patch = subprocess.run(["git", "show", "--no-color", "--no-ext-diff", "--patch", sha], cwd=self.dir,
                                   capture_output=True, text=True).stdout
            pid = subprocess.run(["git", "patch-id", "--stable"], cwd=self.dir, input=patch,
                                 capture_output=True, text=True).stdout.split()
            out.append({"sha": sha, "patch_id": pid[0] if pid else None, "subject": ""})
        return out

    def receipt(self, base: str, head: str = "HEAD", *, state: str = "complete", findings=(), rid: str | None = None,
                reasons=()) -> str:
        """Append a review receipt in the shape review-hub writes."""
        n = len([e for e in gate.load_events(self.dir) if e["type"] == "review"]) + 1
        rid = rid or f"rv-fixture-{n}"
        gate.append_event(self.dir, {
            "type": "review", "id": rid, "at": "2026-09-29T00:00:00Z",
            "repo": {"toplevel": str(self.dir), "origin": None},
            "base": self.git("rev-parse", base), "head": self.git("rev-parse", head),
            "commits": self.commits(base, head), "surface": "openrouter deepseek-v4.1-flash",
            "runs": {"blind": "b", "final": "f"}, "state": state, "state_reasons": list(reasons),
            "partial_phases": [], "verdict": "ready", "findings": list(findings),
        })
        return rid

    def merge_receipt(self, sha: str, *, state: str = "complete", findings=(), reasons=(), flagged: bool = True) -> str:
        """Append the receipt `hatch review --merge <sha>` writes: one commit entry, keyed to the merge SHA, flagged `merge`."""
        n = len([e for e in gate.load_events(self.dir) if e["type"] == "review"]) + 1
        rid = f"rv-fixture-merge-{n}"
        parents = self.git("rev-list", "--parents", "-n", "1", sha).split()[1:]
        entry = {"sha": sha, "patch_id": None, "subject": "", "parents": parents}
        if flagged:
            entry["merge"] = True
        gate.append_event(self.dir, {
            "type": "review", "id": rid, "at": "2026-09-30T00:00:00Z", "mode": "merge",
            "repo": {"toplevel": str(self.dir), "origin": None},
            "base": parents[0], "head": sha, "commits": [entry], "surface": "openrouter deepseek-v4.1-flash",
            "runs": {"blind": "b", "final": "f"}, "state": state, "state_reasons": list(reasons),
            "partial_phases": [], "verdict": "ready", "findings": list(findings),
        })
        return rid

    def conflicted_merge(self, path: str = "server/zerg/auth/tokens.py", resolution: str | None = "resolved by hand\n",
                         branch: str = "topic") -> dict:
        """A real conflict. main and a topic branch both rewrite line 3 of `path`; the topic merges main and
        resolves it (`resolution` is the file's whole content; None takes main's side wholesale). Leaves the
        topic branch checked out and origin/main on main's commit."""
        text = "".join(f"line {i}\n" for i in range(10))
        fork = self.commit("add tokens", {path: text})
        self.git("update-ref", "refs/remotes/origin/main", fork)
        main_edit = self.commit("main edits line 3", {path: text.replace("line 3", "main 3")})
        self.git("update-ref", "refs/remotes/origin/main", main_edit)
        self.git("checkout", "-q", "-b", branch, fork)
        topic = self.commit("topic edits line 3", {path: text.replace("line 3", "topic 3")})
        merged = subprocess.run(["git", "merge", "--no-ff", "-m", "Merge main into topic", "main"], cwd=self.dir, capture_output=True, text=True)
        assert merged.returncode != 0 and "CONFLICT" in merged.stdout, merged.stdout  # the fixture is a real conflict
        (self.dir / path).write_text(resolution if resolution is not None else text.replace("line 3", "main 3"))
        self.git("add", path)
        self.git("commit", "-q", "--no-edit")
        return {"fork": fork, "main": main_edit, "topic": topic, "merge": self.git("rev-parse", "HEAD")}

    def clean_merge_of_one_file(self, path: str = "server/zerg/auth/tokens.py") -> dict:
        """main and a topic branch edit different lines of one file: git merges it with no conflict, and the result
        differs from both parents as a whole (which is what a file-level `--cc` listing mistakes for a resolution)."""
        text = "".join(f"line {i}\n" for i in range(10))
        fork = self.commit("add tokens", {path: text})
        self.git("update-ref", "refs/remotes/origin/main", fork)
        main_edit = self.commit("main edits line 1", {path: text.replace("line 1", "main 1")})
        self.git("update-ref", "refs/remotes/origin/main", main_edit)
        self.git("checkout", "-q", "-b", "topic", fork)
        topic = self.commit("topic edits line 8", {path: text.replace("line 8", "topic 8")})
        self.git("merge", "-q", "--no-ff", "-m", "Merge main into topic", "main")
        return {"fork": fork, "main": main_edit, "topic": topic, "merge": self.git("rev-parse", "HEAD")}

    def disposition(self, rid: str, finding: str, disposition: str) -> None:
        gate.append_event(self.dir, {"type": "disposition", "receipt": rid, "finding": finding,
                                     "disposition": disposition, "reason": "because", "by": "test"})

    def policy(self) -> "gate.Policy":
        path = self.dir / "policy.toml"
        path.write_text(FIXTURE_POLICY)
        return gate.Policy.load(path, "fixture")

    def run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        policy = self.dir / "policy.toml"
        policy.write_text(FIXTURE_POLICY)
        full_env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS", gate.OVERRIDE_ENV, gate.OVERRIDE_REASON_ENV)}
        full_env.update(env or {})
        return subprocess.run([sys.executable, str(GATE), "--repo", str(self.dir), "--policy", str(policy), "--name", "fixture", *args],
                              capture_output=True, text=True, env=full_env)


def finding(fid="F1", severity="blocking", summary="wrong"):
    return {"id": fid, "severity": severity, "where": "x:1", "summary": summary}


class GlobTests(unittest.TestCase):
    def test_glob_semantics(self):
        m = lambda pat, path: bool(gate.compile_glob(pat).match(path))  # noqa: E731
        self.assertTrue(m("server/zerg/auth/**", "server/zerg/auth/x/y.py"))
        self.assertFalse(m("server/zerg/auth/**", "server/zerg/authz.py"))
        self.assertTrue(m("**/*.md", "README.md"))
        self.assertTrue(m("**/*.md", "a/b/c.md"))
        self.assertFalse(m("*.md", "a/b.md"))  # `*` stays inside a segment
        self.assertTrue(m("engine/src/*binding*.rs", "engine/src/unmanaged_bindings.rs"))
        self.assertFalse(m("engine/src/*binding*.rs", "engine/src/state/session_binding.rs"))
        self.assertTrue(m("scripts/ops/promote-*.sh", "scripts/ops/promote-dogfood.sh"))
        self.assertTrue(m("a?c", "abc") and not m("a?c", "a/c"))
        self.assertFalse(m("a.b", "axb"))  # `.` is literal


class EnforcementWiringTests(unittest.TestCase):
    """Every script that pushes to main asks the gate first, and the gate's own path is on the list."""

    def source(self, name):
        return (ROOT / "scripts" / "ops" / name).read_text()

    def test_every_pushing_script_calls_the_gate_before_it_pushes(self):
        ship = self.source("ship.sh")
        self.assertLess(ship.index("review_gate.py"), ship.index('push origin "$SHA:refs/heads/$BRANCH"'))
        release = self.source("release.sh")
        self.assertLess(release.index("review_gate.py"), release.index('git -C "$ROOT" push'))
        self.assertIn("review_gate.py", self.source("check-push-readiness.sh"))

    def test_promotions_ask_before_they_change_anything(self):
        dogfood = self.source("promote-dogfood.sh")
        self.assertLess(dogfood.index("lh_review_gate_promotion"), dogfood.index("deploy_json="))
        production = self.source("promote-production.sh")
        gate_at = production.index("lh_review_gate_promotion")
        self.assertLess(gate_at, production.index("lh_hosted_reprovision_production"))
        self.assertLess(gate_at, production.index('ssh "$DEMO_SSH_HOST"'))
        self.assertLess(gate_at, production.index('if [[ "$CHECK_ONLY" == "1" ]]'))  # --check asks too

    def test_the_enforcement_path_is_itself_blocking(self):
        policy = gate.Policy.load(POLICY, "longhouse")
        for path in ("scripts/ops/ship.sh", "scripts/ops/release.sh", "scripts/ops/check-push-readiness.sh",
                     "scripts/ops/promote-dogfood.sh", "scripts/ops/promote-production.sh",
                     "scripts/lib/review-gate.sh", "scripts/ops/review_gate.py", "scripts/ops/review-policy.toml",
                     "scripts/ops/promotion_gates.py", "scripts/ops/install-push-gate.sh",
                     "scripts/ops/ring_promoter.py", "scripts/ops/install-review-attester.sh",
                     ".github/workflows/promote-rings.yml"):
            self.assertTrue(policy.blocking_areas([path]), f"{path} must be on the blocking list")


class PrePushHookTests(unittest.TestCase):
    """A bare `git push origin HEAD:main` asks the gate too: the hole three unreviewed commits landed through."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base_dir = Path(self.tmp.name)
        self.repo = self.clone("longhouse")  # the policy table is the origin URL's basename
        self.base = self.repo.commit("base", {"README.md": "x"})
        self.git_push("origin", "main")  # before the hook exists, so it seeds the remote
        installed = subprocess.run(["bash", str(INSTALLER)], cwd=self.repo.dir, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def clone(self, name):
        remote = self.base_dir / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        work = self.base_dir / f"work-{name}"
        work.mkdir()
        repo = Repo(str(work))
        repo.git("remote", "set-url", "origin", str(remote))
        # The hook runs the checkout's own gate and policy, like a worktree of the real repository does.
        ops = work / "scripts" / "ops"
        ops.mkdir(parents=True)
        for source in (GATE, POLICY):
            (ops / source.name).write_bytes(source.read_bytes())
        return repo

    def git_push(self, *args, repo=None):
        repo = repo or self.repo
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS", gate.OVERRIDE_ENV, gate.OVERRIDE_REASON_ENV)}
        return subprocess.run(["git", "push", *args], cwd=repo.dir, capture_output=True, text=True, env=env)

    def remote_main(self, name="longhouse"):
        return subprocess.run(["git", "--git-dir", str(self.base_dir / f"{name}.git"), "rev-parse", "main"],
                              capture_output=True, text=True).stdout.strip()

    def test_an_unreviewed_blocking_commit_cannot_be_pushed_to_main(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        pushed = self.git_push("origin", "HEAD:main")
        self.assertNotEqual(pushed.returncode, 0)
        self.assertIn("REFUSED push", pushed.stderr)
        self.assertIn("no review receipt", pushed.stderr)
        self.assertEqual(self.remote_main(), self.base, "the refused push moved nothing")

    def test_a_reviewed_blocking_commit_lands_quietly(self):
        head = self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.repo.receipt(self.base)
        pushed = self.git_push("origin", "HEAD:main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(pushed.stdout, "")
        self.assertNotIn("push OK", pushed.stdout + pushed.stderr)  # (the fixture tree lacks most globs' files, so a dead-glob warning is expected)
        self.assertEqual(self.remote_main(), head)

    def test_a_docs_only_push_is_not_asked_for_anything(self):
        head = self.repo.commit("docs", {"docs/notes.md": "words"})
        pushed = self.git_push("origin", "HEAD:main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self.remote_main(), head)

    def test_a_topic_branch_is_not_gated_but_main_is_even_when_it_is_not_the_first_ref(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.assertEqual(self.git_push("origin", "HEAD:refs/heads/topic").returncode, 0)
        refused = self.git_push("origin", "HEAD:refs/heads/topic2", "HEAD:main")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("REFUSED push", refused.stderr)
        self.assertEqual(self.remote_main(), self.base)

    def test_an_unreviewed_commit_on_top_of_a_reviewed_one_is_still_refused(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        reviewed_through = self.repo.git("rev-parse", "HEAD")
        self.repo.receipt(self.base)
        self.repo.commit("more auth", {"server/zerg/auth/other.py": "2"})
        refused = self.git_push("origin", "HEAD:main")
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn(reviewed_through[:12], refused.stderr)  # only the commit without a receipt is named

    def test_the_logged_override_lets_it_through(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        env = {**os.environ, gate.OVERRIDE_ENV: "david", gate.OVERRIDE_REASON_ENV: "hotfix"}
        pushed = subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=self.repo.dir, capture_output=True, text=True, env=env)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertIn("OVERRIDDEN", pushed.stderr)

    def test_a_gate_that_cannot_decide_does_not_stop_every_push(self):
        # No policy table for this repository name: the gate answers exit 2, the hook says so and allows.
        other = self.clone("nopolicy")
        head = other.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        installed = subprocess.run(["bash", str(INSTALLER)], cwd=other.dir, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        pushed = self.git_push("origin", "HEAD:main", repo=other)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertIn("could not decide", pushed.stderr)
        self.assertEqual(self.remote_main("nopolicy"), head)

    def test_a_malformed_or_missing_policy_is_a_gate_fault_not_a_refusal(self):
        # An uncaught exception exits 1, which the hook would read as "refused" and stop every push on.
        gate_dir = self.repo.dir / "scripts" / "ops"
        (gate_dir / "review-policy.toml").write_text("this is [not toml")
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        direct = subprocess.run([sys.executable, str(gate_dir / "review_gate.py"), "--repo", str(self.repo.dir), "push", "--base", self.base],
                                capture_output=True, text=True)
        self.assertEqual(direct.returncode, 2)
        self.assertIn("cannot read the review policy", direct.stderr)
        self.assertNotIn("Traceback", direct.stderr)
        pushed = self.git_push("origin", "HEAD:main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertIn("could not decide", pushed.stderr)
        (gate_dir / "review-policy.toml").unlink()
        self.repo.commit("more auth", {"server/zerg/auth/other.py": "2"})
        self.assertEqual(self.git_push("origin", "HEAD:main").returncode, 0)

    def test_an_internal_error_in_the_gate_is_exit_2_not_a_refusal(self):
        # No git on PATH: subprocess raises FileNotFoundError, which is not a GateError.
        result = subprocess.run([sys.executable, str(GATE), "--repo", str(self.repo.dir), "--name", "longhouse", "push"],
                                capture_output=True, text=True, env={"PATH": ""})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("could not", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_push_whose_remote_main_was_never_fetched_still_asks_about_every_unpublished_commit(self):
        # remote_sha is unknown to this checkout and no remote-tracking ref exists: the range is "what no remote has".
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        head = self.repo.git("rev-parse", "HEAD")
        line = f"refs/heads/main {head} refs/heads/main {'1' * 40}\n"
        result = subprocess.run([sys.executable, str(GATE), "--repo", str(self.repo.dir), "pre-push", "origin"],
                                input=line, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("no review receipt", result.stderr)
        self.assertIn(head[:12], result.stderr)

    def test_the_unfetched_fallback_still_counts_a_commit_that_sits_on_a_topic_branch(self):
        # A commit some remote topic branch holds is about to reach main; it is not "already published".
        head = self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.repo.git("update-ref", "refs/remotes/origin/topic", head)
        line = f"refs/heads/main {head} refs/heads/main {'1' * 40}\n"
        result = subprocess.run([sys.executable, str(GATE), "--repo", str(self.repo.dir), "pre-push", "origin"],
                                input=line, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(head[:12], result.stderr)

    def test_a_topic_branch_push_prints_nothing_at_all(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        pushed = self.git_push("origin", "HEAD:refs/heads/topic")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertNotIn("review-gate", pushed.stderr)  # not even the dead-glob warning

    def test_a_gate_that_does_not_even_import_is_a_fault_not_a_refusal(self):
        (self.repo.dir / "scripts" / "ops" / "review_gate.py").write_text("def broken(:\n")
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        pushed = self.git_push("origin", "HEAD:main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertIn("could not decide", pushed.stderr)

    def test_a_missing_python_does_not_stop_pushes_either(self):
        bin_dir = self.base_dir / "only-git"
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS", gate.OVERRIDE_ENV, gate.OVERRIDE_REASON_ENV)}
        env["PATH"] = str(bin_dir)
        pushed = subprocess.run([str(bin_dir / "git"), "push", "origin", "HEAD:main"], cwd=self.repo.dir, capture_output=True, text=True, env=env)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertIn("python3 is not on PATH", pushed.stderr)

    def test_the_installer_is_idempotent_and_never_replaces_a_hook_that_is_not_its_own(self):
        again = subprocess.run(["bash", str(INSTALLER)], cwd=self.repo.dir, capture_output=True, text=True)
        self.assertEqual(again.returncode, 0, again.stderr)
        hook = Path(self.repo.git("rev-parse", "--path-format=absolute", "--git-path", "hooks")) / "pre-push"
        self.assertTrue(os.access(hook, os.X_OK))
        hook.write_text("#!/bin/sh\necho someone else's hook\n")
        refused = subprocess.run(["bash", str(INSTALLER)], cwd=self.repo.dir, capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("someone else's hook", hook.read_text())

    def test_push_readiness_says_where_a_clone_lacks_the_hook(self):
        self.assertIn("make install-push-gate", (ROOT / "scripts" / "ops" / "check-push-readiness.sh").read_text())

    def test_the_makefile_offers_the_installer(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn("install-push-gate: ##", makefile)
        self.assertIn("./scripts/ops/install-push-gate.sh", makefile)


class RepoNameTests(unittest.TestCase):
    def test_policy_table_comes_from_the_origin_url(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repo(directory)
            for url, name in (("git@github.com:cipher982/longhouse.git", "longhouse"),
                              ("https://github.com/cipher982/longhouse-control-plane.git", "longhouse-control-plane"),
                              ("https://github.com/cipher982/longhouse/", "longhouse")):
                repo.git("remote", "set-url", "origin", url)
                self.assertEqual(gate.repo_name(directory), name)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.tmp.name)
        self.policy = self.repo.policy()

    def tearDown(self):
        self.tmp.cleanup()

    def test_exempt_commits(self):
        exempt = self.policy.exempt_commit
        self.assertTrue(exempt("docs", ["docs/a.txt", "README.md"]))
        self.assertTrue(exempt("tests", ["server/tests_lite/test_a.py", "server/x/test_b.py"]))
        self.assertTrue(exempt("empty commit", []))
        self.assertFalse(exempt("docs and code", ["README.md", "server/a.py"]))

    def test_release_bump_needs_the_subject_and_only_bump_files(self):
        exempt = self.policy.exempt_commit
        self.assertTrue(exempt("Bump version to 0.1.61", ["server/pyproject.toml", ".bumpversion.toml"]))
        self.assertFalse(exempt("Bump version to 0.1.61", ["server/pyproject.toml", "server/zerg/auth/x.py"]))
        self.assertFalse(exempt("Bump dependency", ["server/pyproject.toml"]))

    def test_a_rename_out_of_a_blocking_path_still_counts_as_touching_it(self):
        repo = self.repo
        base = repo.commit("base", {"server/zerg/auth/tokens.py": "x = 1\n" * 40})
        repo.git("update-ref", "refs/remotes/origin/main", base)
        repo.git("mv", "server/zerg/auth/tokens.py", "server/zerg/security_tokens.py")
        repo.git("commit", "-q", "-m", "move tokens out of auth")
        (commit,) = gate.commits_in(self.tmp.name, f"{base}..HEAD")
        self.assertIn("server/zerg/auth/tokens.py", commit.files)
        self.assertEqual(self.policy.blocking_areas(commit.files), ["auth"])
        # and a code file renamed into docs is not exempt
        (Path(self.tmp.name) / "docs").mkdir()
        repo.git("mv", "server/zerg/security_tokens.py", "docs/tokens.md")
        repo.git("commit", "-q", "-m", "hide code in docs")
        (last,) = gate.commits_in(self.tmp.name, "HEAD~1..HEAD")
        self.assertFalse(self.policy.exempt_commit(last.subject, last.files))

    def test_blocking_areas(self):
        areas = self.policy.blocking_areas
        self.assertEqual(areas(["server/zerg/auth/tokens.py"]), ["auth"])
        self.assertEqual(areas(["engine/src/state/db.rs", "server/zerg/auth/a.py"]), ["auth", "engine state"])
        self.assertEqual(areas(["server/zerg/services/x.py"]), [])

    def test_the_checked_in_policy_loads_for_both_repos_and_every_blocking_glob_matches_a_file(self):
        for name in ("longhouse", "longhouse-control-plane"):
            policy = gate.Policy.load(POLICY, name)
            self.assertTrue(policy.blocking, name)
        tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")
        if len(tracked) < 100:  # not a full checkout (a source tarball, a guest snapshot)
            self.skipTest("no tracked file list")
        data = __import__("tomllib").loads(POLICY.read_text())
        dead = [(entry["area"], p) for entry in data["repos"]["longhouse"]["blocking"] for p in entry["paths"]
                if not any(gate.compile_glob(p).match(f) for f in tracked)]
        self.assertEqual(dead, [], "blocking globs that match no tracked file: fix review-policy.toml (a rename must not switch a protection off)")


class PushRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.tmp.name)
        self.base = self.repo.commit("base", {"README.md": "x"})
        self.repo.git("update-ref", "refs/remotes/origin/main", self.base)
        self.policy = self.repo.policy()

    def tearDown(self):
        self.tmp.cleanup()

    def push(self):
        return gate.push_verdicts(self.tmp.name, self.policy, "origin/main")

    def test_reversible_change_lands_without_a_review(self):
        self.repo.commit("feature", {"server/zerg/services/thing.py": "1"})
        self.assertEqual(self.push(), [])

    def test_blocking_commit_without_a_receipt_is_refused_with_its_area(self):
        sha = self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        (verdict,) = self.push()
        self.assertEqual((verdict.commit.sha, verdict.areas, verdict.reasons), (sha, ["auth"], ["no review receipt"]))

    def test_only_the_blocking_commits_of_a_range_are_listed(self):
        self.repo.commit("feature", {"server/zerg/services/thing.py": "1"})
        blocking = self.repo.commit("engine", {"engine/src/state/db.rs": "1"})
        self.repo.commit("docs", {"docs/a.md": "1"})
        self.assertEqual([v.commit.sha for v in self.push()], [blocking])

    def test_completed_receipt_lets_it_land(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.repo.receipt("origin/main")
        self.assertEqual(self.push(), [])

    def test_partial_receipt_is_not_a_completed_review(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.repo.receipt("origin/main", state="partial", reasons=["pass hit its time budget: final"])
        (verdict,) = self.push()
        self.assertIn("only a partial review", verdict.reasons[0])
        self.assertIn("time budget", verdict.reasons[0])

    def test_unresolved_material_finding_blocks_until_fixed_or_rejected(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        rid = self.repo.receipt("origin/main", findings=[finding("F1", "material"), finding("F2", "minor")])
        (verdict,) = self.push()
        self.assertIn(f"unresolved material finding {rid} F1", verdict.reasons[0])
        self.repo.disposition(rid, "F1", "deferred")
        self.assertEqual(len(self.push()), 1)  # deferred does not resolve
        self.repo.disposition(rid, "F1", "rejected")
        self.assertEqual(self.push(), [])  # the minor finding never gated

    def test_a_rebased_commit_keeps_its_review(self):
        self.repo.git("checkout", "-q", "-b", "topic")
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.repo.receipt("origin/main")
        # main moves; the topic is rebased onto it: new SHA, same patch
        self.repo.git("checkout", "-q", "main")
        moved = self.repo.commit("someone else", {"other.txt": "1"})
        self.repo.git("update-ref", "refs/remotes/origin/main", moved)
        self.repo.git("checkout", "-q", "topic")
        self.repo.git("rebase", "-q", "origin/main")
        self.assertEqual(self.push(), [])

    def test_a_partial_review_of_the_old_sha_does_not_hide_a_complete_review_of_the_rebased_patch(self):
        self.repo.git("checkout", "-q", "-b", "topic")
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.repo.receipt("origin/main", state="partial", reasons=["pass hit its time budget: final"])
        self.repo.git("checkout", "-q", "main")
        moved = self.repo.commit("someone else", {"other.txt": "1"})
        self.repo.git("update-ref", "refs/remotes/origin/main", moved)
        self.repo.git("checkout", "-q", "topic")
        self.repo.git("rebase", "-q", "origin/main")
        self.assertEqual(len(self.push()), 1)  # only the partial one so far
        self.repo.receipt("origin/main")  # a complete review of the rebased commit
        self.assertEqual(self.push(), [])

    def test_a_commit_changed_after_review_is_not_covered(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.repo.receipt("origin/main")
        (Path(self.tmp.name) / "server/zerg/auth/tokens.py").write_text("2")
        self.repo.git("commit", "-q", "-a", "--amend", "-m", "auth change")
        self.assertEqual(len(self.push()), 1)

    def test_a_receipt_for_a_shorter_range_covers_only_those_commits(self):
        first = self.repo.commit("auth one", {"server/zerg/auth/a.py": "1"})
        self.repo.receipt("origin/main")
        second = self.repo.commit("auth two", {"server/zerg/auth/b.py": "1"})
        self.assertEqual([v.commit.sha for v in self.push()], [second])
        self.assertNotEqual(first, second)

    # --- merge commits: judged by what they add to a clean merge of their parents (git show --remerge-diff) ---

    def merges(self):
        return [v for v in self.push() if v.commit.merge]

    def test_a_clean_merge_asks_for_nothing(self):
        repo = self.repo
        repo.git("checkout", "-q", "-b", "side")
        repo.commit("side work", {"docs/side.md": "1", "server/zerg/auth/tokens.py": "side\n"})
        repo.git("checkout", "-q", "main")
        repo.commit("main work", {"docs/main.md": "1"})
        repo.git("update-ref", "refs/remotes/origin/main", "main")
        repo.git("checkout", "-q", "-b", "topic", "main")
        repo.git("merge", "-q", "--no-ff", "-m", "clean merge", "side")  # brings a reviewed-elsewhere auth change in
        (clean,) = [c for c in gate.commits_in(self.tmp.name, "main..topic") if c.merge]
        self.assertEqual(clean.files, [])
        self.assertEqual(self.merges(), [])

    def test_a_clean_merge_of_two_edits_to_one_file_is_not_mistaken_for_a_resolution(self):
        # The regression: landed merge dd48da29b took both sides' edits to one generated JSON file with no
        # conflict, and a file-level `--cc` listing called that "conflict resolution" nothing could ever clear.
        merge = self.repo.clean_merge_of_one_file()["merge"]
        (commit,) = [c for c in gate.commits_in(self.tmp.name, "origin/main..topic") if c.merge]
        self.assertEqual((commit.sha, commit.files), (merge, []))
        self.assertEqual(self.merges(), [])
        result = self.repo.run("status", "--range", "origin/main..topic")
        self.assertIn("exempt", [line for line in result.stdout.splitlines() if line.startswith(merge[:12])][0])

    def test_a_merge_with_a_conflict_resolution_needs_a_receipt_of_its_own(self):
        m = self.repo.conflicted_merge()
        self.repo.receipt(m["fork"], m["topic"])  # the commits it brings in, reviewed as a range
        (verdict,) = self.merges()
        self.assertEqual((verdict.commit.sha, verdict.commit.files, verdict.areas), (m["merge"], ["server/zerg/auth/tokens.py"], ["auth"]))
        self.assertTrue(verdict.needs_receipt)
        self.assertIn("no review receipt for the merge's own changes", verdict.reasons[0])
        # the range receipt does not cover it, and neither does an entry that was not written by `--merge`
        self.assertEqual(len(self.merges()), 1)
        self.repo.merge_receipt(m["merge"], flagged=False)
        self.assertEqual(len(self.merges()), 1)
        self.repo.merge_receipt(m["merge"])
        self.assertEqual(self.merges(), [])

    def test_a_merge_receipt_is_keyed_to_the_exact_merge(self):
        m = self.repo.conflicted_merge()
        self.repo.receipt(m["fork"], m["topic"])
        rid = self.repo.merge_receipt(m["merge"])
        self.assertEqual(self.push(), [])
        # re-resolve the same conflict differently: a new merge SHA, so the old review does not cover it
        self.repo.git("reset", "-q", "--hard", m["topic"])
        subprocess.run(["git", "merge", "--no-ff", "-m", "Merge main into topic", "main"], cwd=self.tmp.name, capture_output=True)
        (Path(self.tmp.name) / "server/zerg/auth/tokens.py").write_text("resolved another way\n")
        self.repo.git("add", "server/zerg/auth/tokens.py")
        self.repo.git("commit", "-q", "--no-edit")
        (verdict,) = self.merges()
        self.assertNotEqual(verdict.commit.sha, m["merge"])
        self.assertIn("no review receipt", verdict.reasons[0])
        self.assertTrue(rid)

    def test_merge_receipts_follow_the_same_state_and_finding_rules(self):
        m = self.repo.conflicted_merge()
        self.repo.receipt(m["fork"], m["topic"])
        self.repo.merge_receipt(m["merge"], state="partial", reasons=["pass hit its time budget: final"])
        (verdict,) = self.merges()
        self.assertIn("only a partial review", verdict.reasons[0])
        rid = self.repo.merge_receipt(m["merge"], findings=[finding("F1", "material", "dropped main's edit")])
        (verdict,) = self.merges()
        self.assertIn(f"unresolved material finding {rid} F1", verdict.reasons[0])
        self.repo.disposition(rid, "F1", "rejected")
        self.assertEqual(self.merges(), [])

    def test_taking_one_side_wholesale_is_still_a_change_of_the_merge(self):
        # Dropping the topic's edit by taking main's file is exactly what a review must see, and a combined diff of the
        # result against its parents cannot (the result equals one parent).
        m = self.repo.conflicted_merge(resolution=None)
        self.assertEqual(self.repo.git("diff", "--name-only", m["main"], m["merge"]), "")
        (verdict,) = self.merges()
        self.assertEqual(verdict.commit.files, ["server/zerg/auth/tokens.py"])

    def test_content_added_by_hand_to_a_clean_merge_is_the_merges_own_change(self):
        m = self.repo.clean_merge_of_one_file()
        (Path(self.tmp.name) / "server/zerg/auth/backdoor.py").parent.mkdir(parents=True, exist_ok=True)
        (Path(self.tmp.name) / "server/zerg/auth/backdoor.py").write_text("allow = True\n")
        self.repo.git("add", "server/zerg/auth/backdoor.py")
        self.repo.git("commit", "-q", "--amend", "--no-edit")
        merge = self.repo.git("rev-parse", "HEAD")
        self.assertNotEqual(merge, m["merge"])
        (verdict,) = self.merges()
        self.assertEqual((verdict.commit.files, verdict.areas), (["server/zerg/auth/backdoor.py"], ["auth"]))

    def test_a_resolution_confined_to_docs_needs_no_review(self):
        m = self.repo.conflicted_merge(path="docs/notes.md", resolution="merged notes\n")
        self.assertEqual(self.merges(), [])  # blocking-list rule: docs are not on it
        self.assertEqual(m["merge"], self.repo.git("rev-parse", "HEAD"))

    def test_an_octopus_merge_cannot_be_measured_so_the_gate_cannot_decide(self):
        repo = self.repo
        for branch in ("a", "b"):
            repo.git("checkout", "-q", "-b", branch, "main")
            repo.commit(branch, {f"docs/{branch}.md": branch})
        repo.git("checkout", "-q", "-b", "octopus", "main")
        repo.git("merge", "-q", "--no-ff", "a", "b", "-m", "octopus")
        with self.assertRaises(gate.GateError) as caught:
            gate.commits_in(self.tmp.name, "main..octopus")
        self.assertIn("octopus", str(caught.exception))
        result = self.repo.run("push", "--base", "main", "--head", "octopus")
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_the_refusal_names_the_merge_review_command_not_a_range_review(self):
        m = self.repo.conflicted_merge()
        self.repo.receipt(m["fork"], m["topic"])
        result = self.repo.run("push", "--base", "origin/main", "--head", "topic")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"hatch review -C {Path(self.tmp.name).resolve()} --merge {m['merge'][:12]}", result.stderr)
        self.assertNotIn("--base", result.stderr)  # the topic commit is covered; only the merge is unreviewed
        self.repo.merge_receipt(m["merge"])
        self.assertEqual(self.repo.run("push", "--base", "origin/main", "--head", "topic").returncode, 0)

    def test_events_the_gate_cannot_index_are_skipped_not_fatal(self):
        gate.append_event(self.tmp.name, {"type": "review", "state": "complete", "commits": [{"sha": "a"}]})  # no id
        gate.append_event(self.tmp.name, {"type": "review", "id": "rv-x", "state": "complete", "commits": "nope"})
        gate.append_event(self.tmp.name, {"type": "disposition", "finding": "F1"})  # no receipt
        self.assertEqual(gate.load_events(self.tmp.name), [])
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.assertEqual(len(self.push()), 1)

    def test_a_dead_blocking_glob_is_a_warning_not_a_refusal(self):
        (Path(self.tmp.name) / "policy.toml").write_text(FIXTURE_POLICY + '\n[[repos.fixture.blocking]]\narea = "gone"\npaths = ["nowhere/**"]\n')
        self.repo.commit("feature", {"server/zerg/services/thing.py": "1"})
        result = subprocess.run([sys.executable, str(GATE), "--repo", self.tmp.name, "--policy", str(Path(self.tmp.name) / "policy.toml"),
                                 "--name", "fixture", "push", "--base", self.base], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("nowhere/**", result.stderr)

    def test_cli_refuses_and_names_the_override_only_in_the_refusal(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        result = self.repo.run("push", "--base", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("REFUSED push", result.stderr)
        self.assertIn("auth change", result.stderr)
        self.assertIn(gate.OVERRIDE_ENV, result.stderr)
        self.assertIn("hatch review disposition", result.stderr)

    def test_an_inherited_ci_variable_does_not_switch_the_rule_off(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        for name in ("CI", "GITHUB_ACTIONS"):
            self.assertEqual(self.repo.run("push", "--base", self.base, env={name: "true"}).returncode, 1, name)

    def test_cli_skips_loudly_without_the_base_ref_and_errors_for_a_repo_with_no_policy_table(self):
        self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        skipped = self.repo.run("push", "--base", "origin/nowhere")
        self.assertEqual(skipped.returncode, 0)
        self.assertIn("skipped", skipped.stderr)
        no_table = subprocess.run(
            [sys.executable, str(GATE), "--repo", str(self.repo.dir), "--policy", str(self.repo.dir / "policy.toml"),
             "--name", "some-fork", "push", "--base", self.base], capture_output=True, text=True)
        self.assertEqual(no_table.returncode, 2)
        self.assertIn("enforce nothing", no_table.stderr)


class PromotionRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.tmp.name)
        self.initial = self.repo.commit("initial", {"README.md": "x"})
        self.repo.commit("ancient unreviewed code", {"server/zerg/old.py": "1"})
        # The commit that adds the policy file: everything before it is grandfathered.
        self.policy_commit = self.repo.commit("add the review policy", {"scripts/ops/review-policy.toml": FIXTURE_POLICY})
        self.served = self.policy_commit
        self.policy = self.repo.policy()

    def tearDown(self):
        self.tmp.cleanup()

    def promote(self, served=None, target="HEAD"):
        return gate.promotion_verdicts(self.tmp.name, self.policy, served or self.served, target)

    def test_nothing_new_promotes(self):
        self.assertEqual(self.promote(), [])

    def test_docs_tests_and_version_bumps_need_no_review(self):
        self.repo.commit("docs", {"docs/a.md": "1"})
        self.repo.commit("tests", {"server/tests_lite/test_a.py": "1"})
        self.repo.commit("Bump version to 0.1.61", {"server/pyproject.toml": "v"})
        self.assertEqual(self.promote(), [])

    def test_a_merge_is_asked_about_only_for_its_own_changes(self):
        clean = self.repo.clean_merge_of_one_file(path="server/zerg/shared.py")
        self.repo.receipt(self.served, clean["merge"])  # every non-merge commit of the range
        self.assertEqual(self.promote(target=clean["merge"]), [])  # the clean merge is not a commit to review
        self.repo.git("checkout", "-q", "main")
        self.repo.git("reset", "-q", "--hard", self.policy_commit)
        m = self.repo.conflicted_merge(path="server/zerg/shared.py", branch="topic2")
        self.repo.receipt(self.served, m["merge"])
        (verdict,) = self.promote(target=m["merge"])
        self.assertEqual((verdict.commit.sha, verdict.commit.merge), (m["merge"], True))
        self.assertTrue(verdict.needs_receipt)
        self.repo.merge_receipt(m["merge"])
        self.assertEqual(self.promote(target=m["merge"]), [])

    def test_every_unreviewed_code_commit_is_listed_not_only_blocking_ones(self):
        a = self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        self.repo.commit("docs", {"docs/a.md": "1"})
        b = self.repo.commit("auth", {"server/zerg/auth/b.py": "1"})
        verdicts = self.promote()
        self.assertEqual([v.commit.sha for v in verdicts], [a, b])
        self.assertEqual(verdicts[1].areas, ["auth"])

    def test_commits_before_the_policy_existed_are_not_asked_for_a_review_but_the_policy_commit_is(self):
        verdicts = self.promote(served=self.initial)
        self.assertEqual([v.commit.sha for v in verdicts], [self.policy_commit])  # not "ancient unreviewed code"

    def test_one_receipt_over_the_range_covers_it(self):
        self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        self.repo.commit("feature b", {"server/zerg/b.py": "1"})
        self.repo.receipt(self.served)
        self.assertEqual(self.promote(), [])

    def test_unresolved_material_finding_blocks_promotion_even_when_review_is_complete(self):
        self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        rid = self.repo.receipt(self.served, findings=[finding("F1", "blocking")])
        (verdict,) = self.promote()
        self.assertIn("unresolved blocking finding", verdict.reasons[0])
        self.repo.disposition(rid, "F1", "fixed")
        self.assertEqual(self.promote(), [])

    def test_a_fix_after_review_needs_the_new_range_reviewed(self):
        self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        rid = self.repo.receipt(self.served, findings=[finding("F1", "material")])
        fix = self.repo.commit("fix the finding", {"server/zerg/a.py": "2"})
        self.repo.disposition(rid, "F1", "fixed")
        self.assertEqual([v.commit.sha for v in self.promote()], [fix])
        self.repo.receipt(self.served)  # re-review of the whole range
        self.assertEqual(self.promote(), [])

    # --- a finding blocks only the commits it is about, and only for history it belongs to ---

    def located(self, fid, severity, path):
        return {"id": fid, "severity": severity, "where": f"{path}:1", "summary": "about " + path}

    def test_a_receipt_of_a_proposal_that_never_landed_has_no_say(self):
        # 2026-10-07: a withdrawn revert was reviewed from a base far behind main, so its receipt listed
        # landed commits too; its finding (about the revert) refused every one of them.
        a = self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        self.repo.receipt(self.served, a)  # a's own, clean
        b = self.repo.commit("feature b", {"server/zerg/b.py": "1"})  # landed with no review of its own yet
        self.repo.git("checkout", "-q", "-b", "proposal")
        revert = self.repo.commit("Revert feature a", {"server/zerg/a.py": "0"})
        self.repo.receipt(self.served, revert, findings=[self.located("F1", "blocking", "server/zerg/a.py")])
        self.repo.git("checkout", "-q", "main")
        self.repo.git("branch", "-q", "-D", "proposal")
        verdicts = self.promote(target=b)
        # a is untouched by the proposal's finding; b gets no coverage from a receipt of history that never landed.
        self.assertEqual([(v.commit.sha, v.reasons) for v in verdicts], [(b, ["no review receipt"])])

    def test_a_clean_review_of_a_proposal_still_covers_the_part_of_it_that_landed(self):
        # 2026-10-07: a reviewed pair of deletions landed one commit at a time; the first had landed alone.
        x = self.repo.commit("delete modules", {"server/zerg/x.py": "1"})
        self.repo.git("checkout", "-q", "-b", "proposal")
        y = self.repo.commit("delete functions", {"server/zerg/y.py": "1"})
        self.repo.receipt(self.served, y)  # x and y, clean
        self.repo.git("checkout", "-q", "main")
        self.repo.git("branch", "-q", "-D", "proposal")
        self.assertEqual(self.promote(target=x), [])
        self.assertNotEqual(x, y)

    def test_a_finding_blocks_only_the_commit_whose_file_it_names(self):
        a = self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        x = self.repo.commit("feature x", {"server/zerg/x.py": "1"})
        c = self.repo.commit("feature c", {"server/zerg/c.py": "1"})
        rid = self.repo.receipt(self.served, c, findings=[self.located("F1", "material", "server/zerg/x.py")])
        (verdict,) = self.promote()
        self.assertEqual(verdict.commit.sha, x)  # still blocks x, the commit it is about
        self.assertIn(f"unresolved material finding {rid} F1", verdict.reasons[0])
        self.assertNotIn(a, [v.commit.sha for v in self.promote()])
        self.assertNotIn(c, [v.commit.sha for v in self.promote()])

    def test_a_finding_that_names_no_reviewed_file_is_about_its_receipt_not_the_whole_range(self):
        a = self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        self.repo.receipt(self.served, a)
        b = self.repo.commit("feature b", {"server/zerg/b.py": "1"})
        c = self.repo.commit("feature c", {"server/zerg/c.py": "1"})
        self.repo.receipt(a, c, findings=[finding("F1", "blocking")])  # where "x:1": no reviewed commit changed x
        self.assertEqual([v.commit.sha for v in self.promote()], [b, c])  # not a

    def test_a_finding_on_a_commit_that_was_later_fixed_and_re_reviewed_no_longer_blocks(self):
        x = self.repo.commit("feature x", {"server/zerg/x.py": "1"})
        old = self.repo.receipt(self.served, x, findings=[self.located("F1", "blocking", "server/zerg/x.py")])
        fix = self.repo.commit("fix x", {"server/zerg/x.py": "2"})
        self.repo.receipt(x, fix)  # the fix alone, clean: says nothing about x
        self.assertEqual([v.commit.sha for v in self.promote()], [x])
        self.repo.receipt(self.served, x)  # x again, alone, clean: a re-run is not a fix
        self.assertEqual([v.commit.sha for v in self.promote()], [x])
        self.repo.receipt(self.served, fix)  # x together with its fix, nothing open about x
        self.assertEqual(self.promote(), [])
        self.assertNotIn(old, [r for v in self.promote() for r in v.reasons])

    def test_a_fix_on_main_does_not_supersede_a_finding_for_a_target_without_the_fix(self):
        # 2026-10-07 (review rv-20261007T200905Z-457bb43-0f3d F1): the clean review read the fix and its head
        # landed on main, but a promotion of the older x does not contain the fix, so x stays blocked.
        x = self.repo.commit("feature x", {"server/zerg/x.py": "1"})
        self.repo.receipt(self.served, x, findings=[self.located("F1", "blocking", "server/zerg/x.py")])
        fix = self.repo.commit("fix x", {"server/zerg/x.py": "2"})
        self.repo.receipt(self.served, fix)  # x with its fix, clean
        self.repo.git("update-ref", "refs/remotes/origin/main", fix)
        self.assertEqual([v.commit.sha for v in self.promote(target=x)], [x])
        self.assertEqual(self.promote(target=fix), [])

    def test_a_later_clean_review_that_reads_no_change_to_the_findings_file_does_not_supersede(self):
        x = self.repo.commit("feature x", {"server/zerg/x.py": "1"})
        self.repo.receipt(self.served, x, findings=[self.located("F1", "blocking", "server/zerg/x.py")])
        other = self.repo.commit("unrelated", {"server/zerg/other.py": "1"})
        self.repo.receipt(self.served, other)  # x plus an unrelated commit, clean: nothing fixed x
        self.assertEqual([v.commit.sha for v in self.promote()], [x])

    def test_a_fix_that_never_landed_supersedes_nothing(self):
        x = self.repo.commit("feature x", {"server/zerg/x.py": "1"})
        self.repo.receipt(self.served, x, findings=[self.located("F1", "blocking", "server/zerg/x.py")])
        self.repo.git("checkout", "-q", "-b", "proposal")
        fix = self.repo.commit("fix x", {"server/zerg/x.py": "2"})
        self.repo.receipt(self.served, fix)  # clean, but the fix is only a proposal
        self.repo.git("checkout", "-q", "main")
        self.repo.git("branch", "-q", "-D", "proposal")
        self.assertEqual([v.commit.sha for v in self.promote(target=x)], [x])

    def test_a_forks_main_is_not_landed_history(self):
        a = self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        self.repo.git("checkout", "-q", "-b", "fork")
        b = self.repo.commit("fork work", {"server/zerg/b.py": "1"})
        self.repo.receipt(self.served, b, findings=[self.located("F1", "blocking", "server/zerg/b.py")])
        self.repo.git("update-ref", "refs/remotes/fork/main", b)
        self.repo.git("checkout", "-q", "main")
        # Not origin's main: the proposal has no say, so a needs its own review rather than b's verdict.
        self.assertEqual([(v.commit.sha, v.reasons) for v in self.promote(target=a)], [(a, ["no review receipt"])])

    def test_a_finding_attributed_to_its_whole_receipt_is_superseded_by_a_review_reaching_past_it(self):
        x = self.repo.commit("feature x", {"server/zerg/x.py": "1"})
        self.repo.receipt(self.served, x, findings=[finding("F1", "blocking")])  # names no file x changed
        fix = self.repo.commit("fix", {"server/zerg/y.py": "1"})
        self.repo.receipt(self.served, fix)
        self.assertEqual(self.promote(), [])

    def test_a_re_review_that_still_finds_the_problem_does_not_supersede(self):
        x = self.repo.commit("feature x", {"server/zerg/x.py": "1"})
        self.repo.receipt(self.served, x, findings=[self.located("F1", "blocking", "server/zerg/x.py")])
        fix = self.repo.commit("attempted fix", {"server/zerg/other.py": "2"})
        self.repo.receipt(self.served, fix, findings=[self.located("F1", "blocking", "server/zerg/x.py")])
        (verdict,) = self.promote()
        self.assertEqual(verdict.commit.sha, x)
        self.assertEqual(len(verdict.reasons), 2)  # both reviews' findings are open about x

    def test_a_review_whose_head_landed_after_the_target_still_covers_it(self):
        a = self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        b = self.repo.commit("feature b", {"server/zerg/b.py": "1"})
        self.repo.receipt(self.served, b, findings=[self.located("F1", "blocking", "server/zerg/b.py")])
        self.repo.git("update-ref", "refs/remotes/origin/main", b)
        self.assertEqual(self.promote(target=a), [])  # landed on main; its finding is about b, not a
        self.repo.git("update-ref", "-d", "refs/remotes/origin/main")
        # No landed history holds b: a proposal with an open finding has no say, so a needs its own review.
        self.assertEqual([(v.commit.sha, v.reasons) for v in self.promote(target=a)], [(a, ["no review receipt"])])

    def test_refusal_lists_the_commits_and_a_receipt_command(self):
        sha = self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        result = self.repo.run("promotion", "--target", "HEAD", "--served", self.served)
        self.assertEqual(result.returncode, 1)
        self.assertIn(sha[:12], result.stderr)
        self.assertIn("feature a", result.stderr)
        self.assertIn("hatch review -C", result.stderr)
        self.assertIn(f"--base {sha[:12]}^ --head {sha[:12]}", result.stderr)
        self.assertIn("David only", result.stderr)

    def test_override_is_david_only_needs_a_reason_and_is_logged(self):
        self.repo.commit("feature a", {"server/zerg/a.py": "1"})
        args = ("promotion", "--target", "HEAD", "--served", self.served)
        self.assertEqual(self.repo.run(*args, env={gate.OVERRIDE_ENV: "an-agent", gate.OVERRIDE_REASON_ENV: "x"}).returncode, 1)
        self.assertEqual(self.repo.run(*args, env={gate.OVERRIDE_ENV: "david"}).returncode, 1)
        ok = self.repo.run(*args, env={gate.OVERRIDE_ENV: "david", gate.OVERRIDE_REASON_ENV: "hotfix, reviewed by hand"})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("OVERRIDDEN", ok.stderr)
        (logged,) = [e for e in gate.load_events(self.tmp.name) if e["type"] == "override"]
        self.assertEqual((logged["by"], logged["gate"], logged["reason"]), ("david", "promotion", "hotfix, reviewed by hand"))
        self.assertEqual(len(logged["commits"]), 1)

    def test_unknown_served_sha_is_an_error_not_a_pass(self):
        result = self.repo.run("promotion", "--target", "HEAD", "--served", "f" * 40)
        self.assertEqual(result.returncode, 2)
        self.assertIn("git fetch", result.stderr)

    def test_served_commit_comes_from_the_health_endpoint(self):
        sha = self.served

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if "Python-urllib" in self.headers.get("User-Agent", ""):
                    # What Cloudflare does to the default agent in front of every longhouse.ai host.
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == "/redirect":
                    self.send_response(308)
                    self.send_header("Location", "/api/health")
                    self.end_headers()
                    return
                body = json.dumps({"status": "healthy", "build": {"commit": sha}} if self.path == "/api/health"
                                  else {"status": "healthy"}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(gate.served_commit(f"{base}/api/health"), sha)
            self.assertEqual(gate.served_commit(f"{base}/redirect"), sha)
            with self.assertRaises(gate.GateError):
                gate.served_commit(f"{base}/no-build")
            self.repo.commit("feature a", {"server/zerg/a.py": "1"})
            refused = self.repo.run("promotion", "--target", "HEAD", "--served-url", f"{base}/api/health")
            self.assertEqual(refused.returncode, 1)
        finally:
            server.shutdown()
            server.server_close()
        with self.assertRaises(gate.GateError):
            gate.served_commit("http://127.0.0.1:9/api/health")

    def test_a_served_url_without_a_scheme_is_a_clean_error_not_a_traceback(self):
        with self.assertRaises(gate.GateError) as caught:
            gate.served_commit("david010.longhouse.ai/api/health")
        self.assertIn("unknown url type", str(caught.exception))
        result = self.repo.run("promotion", "--target", "HEAD", "--served-url", "david010.longhouse.ai/api/health")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not read the served commit", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class AutoReviewTests(unittest.TestCase):
    """Reviews start themselves after a push and on a refused promotion, deduplicated, bounded, retried once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "repo").mkdir()
        self.repo = Repo(str(self.dir / "repo"))
        self.fake = self.dir / "hatch"
        self.fake.write_text(FAKE_HATCH)
        self.fake.chmod(0o755)
        self.calls = self.dir / "calls.jsonl"
        self.calls.touch()
        self.env = {"LONGHOUSE_AUTOREVIEW_HATCH": str(self.fake), "FAKE_HATCH_CALLS": str(self.calls),
                    "FAKE_HATCH_GATE": str(GATE), "FAKE_HATCH_PLAN": str(self.dir / "plan")}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self.restore_env)
        self.started = []
        real_start = gate.start_workers
        self.addCleanup(setattr, gate, "start_workers", real_start)
        gate.start_workers = lambda repo, count: self.started.append(count)  # in-process tests drive the worker themselves
        self.base = self.repo.commit("base", {"README.md": "x"})

    def restore_env(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def calls_made(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def jobs(self):
        return gate._load_jobs(self.repo.dir)

    def test_one_job_per_contiguous_run_of_unreviewed_commits_docs_and_reviewed_ones_split_it(self):
        a = self.repo.commit("a", {"server/a.py": "1"})
        b = self.repo.commit("b", {"server/b.py": "1"})
        self.repo.commit("docs", {"docs/x.md": "1"})
        d = self.repo.commit("d", {"server/d.py": "1"})
        self.repo.receipt(f"{d}^", d)  # d is reviewed
        e = self.repo.commit("e", {"server/e.py": "1"})
        jobs = gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="push")
        self.assertEqual([(j["base"], j["head"], j["commits"]) for j in jobs], [(self.base, b, [a, b]), (d, e, [e])])
        self.assertEqual(self.started, [2])

    def test_a_long_run_is_split_so_each_review_fits_its_budget(self):
        for i in range(gate.AUTO_MAX_RUN + 3):
            self.repo.commit(f"c{i}", {f"server/c{i}.py": "1"})
        jobs = gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="push")
        self.assertEqual([len(j["commits"]) for j in jobs], [gate.AUTO_MAX_RUN, 3])

    def test_the_same_work_is_never_queued_twice_even_after_a_rebase(self):
        self.repo.commit("a", {"server/a.py": "1"})
        policy = self.repo.policy()
        self.assertEqual(len(gate.enqueue_reviews(self.repo.dir, policy, [f"{self.base}..HEAD"], session=None, reason="push")), 1)
        self.assertEqual(gate.enqueue_reviews(self.repo.dir, policy, [f"{self.base}..HEAD"], session=None, reason="push"), [])
        # The push lost a race: the same patch, rebased onto a newer main, is pushed again.
        self.repo.git("checkout", "-q", "-b", "other", self.base)
        other = self.repo.commit("other", {"docs/o.md": "1"})
        self.repo.git("cherry-pick", "main")
        self.assertEqual(gate.enqueue_reviews(self.repo.dir, policy, [f"{other}..HEAD"], session=None, reason="push"), [])
        self.assertEqual(len(self.jobs()), 1)

    def test_nothing_is_queued_when_hatch_is_absent_or_switched_off(self):
        self.repo.commit("a", {"server/a.py": "1"})
        os.environ["LONGHOUSE_AUTOREVIEW_HATCH"] = "off"
        self.assertEqual(gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="push"), [])
        self.assertEqual(self.started, [])

    def test_the_worker_writes_a_receipt_and_retries_a_partial_review_once(self):
        a = self.repo.commit("a", {"server/a.py": "1"})
        (self.dir / "plan").write_text("partial\ncomplete\n")
        gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session="sess-1", reason="push")
        delay = gate.AUTO_RETRY_DELAY_S
        self.addCleanup(setattr, gate, "AUTO_RETRY_DELAY_S", delay)
        gate.AUTO_RETRY_DELAY_S = 0
        self.assertEqual(gate.autoreview_worker(self.repo.dir), 0)
        [job] = self.jobs()
        self.assertEqual((job["state"], job["attempts"]), ("done", 2))
        self.assertEqual([h["receipt_state"] for h in job["history"]], ["partial", "complete"])
        calls = self.calls_made()
        self.assertEqual(calls[0][:3], ["review", "-C", str(self.repo.dir)])
        self.assertIn("--session", calls[0])  # the pusher's session carries the intent
        self.assertEqual(calls[0][calls[0].index("--base") + 1:calls[0].index("--head") + 2], [self.base, "--head", a])
        self.assertFalse(gate.check_commits(self.repo.dir, gate.commits_in(self.repo.dir, f"{self.base}..HEAD"),
                                            gate.load_events(self.repo.dir))[0].needs_receipt)

    def test_a_job_that_never_gets_a_complete_receipt_fails_after_its_retry(self):
        self.repo.commit("a", {"server/a.py": "1"})
        (self.dir / "plan").write_text("partial\npartial\n")
        gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="push")
        delay = gate.AUTO_RETRY_DELAY_S
        self.addCleanup(setattr, gate, "AUTO_RETRY_DELAY_S", delay)
        gate.AUTO_RETRY_DELAY_S = 0
        gate.autoreview_worker(self.repo.dir)
        [job] = self.jobs()
        self.assertEqual((job["state"], job["attempts"]), ("failed", gate.AUTO_MAX_ATTEMPTS))
        self.assertTrue(all("--no-intent" in c for c in self.calls_made()))
        # A failed job does not block a later trigger from trying again.
        self.assertEqual(len(gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="promotion")), 1)

    def test_a_worker_with_no_free_slot_exits_at_once(self):
        self.repo.commit("a", {"server/a.py": "1"})
        gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="push")
        held = [gate._take_slot(self.repo.dir) for _ in range(gate.AUTO_SLOTS)]
        self.addCleanup(lambda: [os.close(fd) for fd in held if fd is not None])
        self.assertTrue(all(fd is not None for fd in held))
        self.assertEqual(gate.autoreview_worker(self.repo.dir), 0)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.jobs()[0]["state"], "queued")

    def test_a_job_whose_worker_died_is_picked_up_again(self):
        self.repo.commit("a", {"server/a.py": "1"})
        gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="push")
        with gate._QueueLock(self.repo.dir):
            jobs = self.jobs()
            jobs[0].update(state="running", pid=2 ** 22 + 12345, attempts=1)
            gate._save_jobs(self.repo.dir, jobs)
        gate.autoreview_worker(self.repo.dir)
        self.assertEqual(self.jobs()[0]["state"], "done")

    def test_a_job_whose_commits_were_reviewed_meanwhile_is_not_run(self):
        self.repo.commit("a", {"server/a.py": "1"})
        gate.enqueue_reviews(self.repo.dir, self.repo.policy(), [f"{self.base}..HEAD"], session=None, reason="push")
        self.repo.receipt(self.base)  # reviewed by hand before a worker got to it
        gate.autoreview_worker(self.repo.dir)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.jobs()[0]["state"], "done")

    def test_a_job_stranded_by_a_dead_worker_gets_a_worker_from_the_next_trigger_or_queue(self):
        self.repo.commit("a", {"server/a.py": "1"})
        policy = self.repo.policy()
        gate.enqueue_reviews(self.repo.dir, policy, [f"{self.base}..HEAD"], session=None, reason="push")
        with gate._QueueLock(self.repo.dir):
            jobs = self.jobs()
            jobs[0].update(state="running", pid=2 ** 22 + 12345, attempts=1)  # its worker died (reboot, OOM)
            gate._save_jobs(self.repo.dir, jobs)
        self.started.clear()
        self.assertEqual(gate.enqueue_reviews(self.repo.dir, policy, [f"{self.base}..HEAD"], session=None, reason="promotion"), [])
        self.assertEqual(self.started, [1], "nothing new, but the requeued job needs a worker")
        self.started.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.queue_mode(str(self.repo.dir), None), 0)
        self.assertEqual(self.started, [1], "the documented wait command starts one too")

    def test_a_refused_promotion_starts_the_missing_reviews_and_says_so(self):
        self.repo.commit("a", {"server/a.py": "1"})
        result = self.repo.run("promotion", "--target", "HEAD", "--served", self.base, "--start-reviews",
                               env={**self.env, "LONGHOUSE_AUTOREVIEW_HATCH": "off"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("No new background review was started", result.stderr)
        self.assertIn("hatch review -C", result.stderr)  # the manual command stays when nothing started
        started = self.repo.run("promotion", "--target", "HEAD", "--served", self.base, "--start-reviews",
                                env={**self.env, "FAKE_HATCH_SLEEP": "1"})
        self.assertEqual(started.returncode, 1, "starting reviews never turns a refusal into a pass")
        self.assertIn("Started 1 background review(s)", started.stderr)
        self.assertIn("queue --wait", started.stderr)
        self.assertNotIn("hatch review -C", started.stderr)
        waited = self.repo.run("queue", "--wait", "60", env=self.env)
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertIn("done", waited.stdout)
        self.assertEqual(self.repo.run("promotion", "--target", "HEAD", "--served", self.base).returncode, 0)

    def test_without_start_reviews_a_promotion_refusal_queues_nothing(self):
        self.repo.commit("a", {"server/a.py": "1"})
        result = self.repo.run("promotion", "--target", "HEAD", "--served", self.base, env=self.env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.jobs(), [])

    def test_the_promote_scripts_ask_for_the_reviews_to_start(self):
        self.assertIn("--start-reviews", (ROOT / "scripts" / "lib" / "review-gate.sh").read_text())


class PushStartsReviewTests(PrePushHookTests.__bases__[0]):
    """The installed hook, a real `git push`: it returns at once and the receipt appears afterwards."""

    def setUp(self):
        PrePushHookTests.setUp(self)
        self.fake = self.base_dir / "hatch"
        self.fake.write_text(FAKE_HATCH)
        self.fake.chmod(0o755)
        self.calls = self.base_dir / "calls.jsonl"
        self.calls.touch()
        self.env = {**{k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS", gate.OVERRIDE_ENV)},
                    "LONGHOUSE_AUTOREVIEW_HATCH": str(self.fake), "FAKE_HATCH_CALLS": str(self.calls),
                    "FAKE_HATCH_GATE": str(GATE), "FAKE_HATCH_SLEEP": "3", "LONGHOUSE_MANAGED_SESSION_ID": "sess-push"}
        self.addCleanup(self.stop_workers)

    clone = PrePushHookTests.clone
    git_push = PrePushHookTests.git_push
    remote_main = PrePushHookTests.remote_main

    def stop_workers(self):
        for job in gate._load_jobs(self.repo.dir):
            if job.get("pid") and gate._alive(job["pid"]):
                os.kill(job["pid"], 15)

    def push(self):
        started = time.monotonic()
        pushed = subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=self.repo.dir, capture_output=True, text=True, env=self.env)
        return pushed, time.monotonic() - started

    def wait_for(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def test_a_push_returns_before_the_review_ends_and_its_receipt_appears_later(self):
        head = self.repo.commit("code", {"server/zerg/x.py": "1"})
        pushed, elapsed = self.push()
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self.remote_main(), head)
        self.assertIn("started 1 background review(s)", pushed.stderr)
        self.assertLess(elapsed, 3, "the push waited for the review")
        covered = lambda: not gate.check_commits(self.repo.dir, gate.commits_in(self.repo.dir, f"{head}^!"),  # noqa: E731
                                                 gate.load_events(self.repo.dir))[0].needs_receipt
        self.assertFalse(covered())
        self.assertTrue(self.wait_for(covered), "no receipt appeared")
        self.assertTrue(self.wait_for(lambda: gate._load_jobs(self.repo.dir)[0]["state"] == "done"))
        [call] = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(call[call.index("--session") + 1], "sess-push")

    def test_pushing_the_same_commits_again_does_not_stack_a_second_review(self):
        head = self.repo.commit("code", {"server/zerg/x.py": "1"})
        self.assertEqual(self.push()[0].returncode, 0)
        # Another clone of the same remote pushes the same range: the job is still queued or running.
        self.repo.git("update-ref", "refs/remotes/origin/main", self.base)
        again = subprocess.run([sys.executable, str(self.repo.dir / "scripts/ops/review_gate.py"), "--repo", str(self.repo.dir),
                                "pre-push", "origin"], input=f"refs/heads/main {head} refs/heads/main {self.base}\n",
                               capture_output=True, text=True, env=self.env)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("started", again.stderr)
        self.assertEqual(len(gate._load_jobs(self.repo.dir)), 1)

    def test_a_docs_push_starts_nothing(self):
        self.repo.commit("docs", {"docs/x.md": "1"})
        pushed, _ = self.push()
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertNotIn("background review", pushed.stderr)
        self.assertEqual(gate._load_jobs(self.repo.dir), [])


class BlockingModeTests(unittest.TestCase):
    """The dogfood fast lane's guard: which commits of a range touch the blocking list, receipts or not."""

    def test_lists_blocking_commits_and_exits_1_and_exits_0_for_a_clean_range(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repo(directory)
            base = repo.commit("base", {"README.md": "x"})
            repo.commit("feature", {"server/zerg/services/thing.py": "1"})
            clean = repo.git("rev-parse", "HEAD")
            auth = repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
            repo.receipt(base)  # a review does not make a range fast-lane: the guard is about paths
            result = repo.run("blocking", "--base", base, "--head", "HEAD")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(auth[:12], result.stdout)
            self.assertIn("[auth]", result.stdout)
            self.assertNotIn(clean[:12], result.stdout)
            result = repo.run("blocking", "--base", base, "--head", clean)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("touches nothing on the blocking list", result.stdout)


class AttestationTests(unittest.TestCase):
    """The promotion verdict published for a promoter that cannot read the receipts, and read back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "repo").mkdir()
        self.repo = Repo(str(Path(self.tmp.name) / "repo"))
        bare = Path(self.tmp.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.repo.git("remote", "set-url", "origin", str(bare))
        self.prod = self.repo.commit("base", {"README.md": "x"})
        self.dogfood = self.repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
        self.target = self.repo.commit("feature", {"server/zerg/services/thing.py": "1"})
        self.repo.git("push", "-q", "origin", "main")
        self.repo.git("fetch", "-q", "origin")
        self.policy = self.repo.policy()
        self.served = {gate.PRODUCTION_HEALTH_URL: self.prod, gate.DOGFOOD_HEALTH_URL: self.dogfood}
        self.posted = []
        patches = {
            "served_commit": lambda url: self.served[url],
            "post_attestation": lambda slug, sha, state, description: self.posted.append((sha, state, description)),
        }
        for name, fn in patches.items():
            original = getattr(gate, name)
            setattr(gate, name, fn)
            self.addCleanup(setattr, gate, name, original)
        os.environ["GITHUB_REPOSITORY"] = "example/fixture"
        self.addCleanup(os.environ.pop, "GITHUB_REPOSITORY", None)

    def attest(self, *targets):
        self.posted.clear()
        rc = gate.attest_mode(str(self.repo.dir), self.policy, list(targets), gate.PRODUCTION_HEALTH_URL,
                              gate.DOGFOOD_HEALTH_URL, dry_run=False, start_reviews=False)
        self.assertEqual(rc, 0)
        return {sha: (state, description) for sha, state, description in self.posted}

    def test_unreviewed_range_attests_failure_from_production(self):
        posted = self.attest(self.target)
        state, description = posted[self.target]
        self.assertEqual(state, "failure")
        self.assertTrue(description.startswith(f"from {self.prod}: 2 commit(s) refused (2 unreviewed)"), description)

    def test_reviewed_range_attests_success_and_an_unchanged_verdict_is_not_posted_again(self):
        self.repo.receipt(self.prod)
        self.assertEqual(self.attest(self.target)[self.target], ("success", f"from {self.prod}: clean"))
        self.assertEqual(self.attest(self.target), {})
        rid = self.repo.receipt(self.prod, rid="rv-later", findings=[finding("F1", "material")])
        self.assertEqual(self.attest(self.target)[self.target][0], "failure")  # a new open finding is posted at once
        self.repo.disposition(rid, "F1", "rejected")
        self.assertEqual(self.attest(self.target)[self.target], ("success", f"from {self.prod}: clean"))

    def test_an_open_finding_dogfood_already_holds_still_allows_a_dogfood_promotion(self):
        rid = self.repo.receipt(self.prod, self.dogfood, findings=[finding("F1", "blocking")])
        self.repo.receipt(self.dogfood)
        (state, description), = self.attest(self.target).values()
        self.assertEqual((state, description), ("success", f"from {self.dogfood}: clean"))
        self.repo.disposition(rid, "F1", "fixed")
        self.assertEqual(self.attest(self.target)[self.target], ("success", f"from {self.prod}: clean"))

    def read_back(self, statuses, served):
        original = gate._github_get
        gate._github_get = lambda path: statuses
        try:
            return gate.attested_refusal(self.repo.dir, "example/fixture", served, self.target)
        finally:
            gate._github_get = original

    def status(self, state, base, login="cipher982", at="2026-10-07T00:00:00Z"):
        return {"context": gate.ATTEST_CONTEXT, "state": state, "description": f"from {base}: x",
                "creator": {"login": login}, "created_at": at}

    def test_reading_back_allows_only_a_success_computed_from_what_the_ring_serves_or_older(self):
        self.assertIsNone(self.read_back([self.status("success", self.prod)], self.prod))
        self.assertIsNone(self.read_back([self.status("success", self.prod)], self.dogfood))  # a sub-range of a clean range
        why = self.read_back([self.status("success", self.dogfood)], self.prod)  # says nothing about prod..dogfood
        self.assertIn("does not contain", why)
        self.assertIn("is failure", self.read_back([self.status("failure", self.prod)], self.prod))
        self.assertIn("no review attestation", self.read_back([], self.prod))
        stray = self.repo.git("commit-tree", "-m", "off main", f"{self.prod}^{{tree}}")  # not contained by target
        self.assertIn("does not contain the served", self.read_back([self.status("success", self.prod)], stray))

    def test_one_failed_post_or_an_unreadable_production_does_not_stop_the_rest(self):
        self.repo.receipt(self.prod)
        calls = []

        def flaky(slug, sha, state, description):
            calls.append(sha)
            if len(calls) == 1:
                raise gate.GateError("gh: network down")
            self.posted.append((sha, state, description))
        gate.post_attestation = flaky
        self.attest(self.dogfood, self.target)
        self.assertEqual(len(calls), 2)
        self.assertEqual([sha for sha, _, _ in self.posted], [self.target])
        self.assertEqual(self.attest(self.dogfood, self.target).keys(), {self.dogfood})  # the failed one is retried
        del self.served[gate.PRODUCTION_HEALTH_URL]
        def unreadable(url):
            if url not in self.served:
                raise gate.GateError("down")
            return self.served[url]
        gate.served_commit = unreadable
        posted = self.attest(self.target)
        self.assertEqual(posted[self.target], ("success", f"from {self.dogfood}: clean"))

    def test_the_newest_attestation_by_an_allowed_account_wins(self):
        statuses = [self.status("success", self.prod, at="2026-10-07T00:00:00Z"),
                    self.status("failure", self.prod, at="2026-10-07T01:00:00Z"),
                    self.status("success", self.prod, login="someone-else", at="2026-10-07T02:00:00Z"),
                    {**self.status("success", self.prod, at="2026-10-07T03:00:00Z"), "context": "ci/other"}]
        self.assertIn("is failure", self.read_back(statuses, self.prod))
        self.assertIn("no review attestation", self.read_back(statuses[2:], self.prod))


class StatusTests(unittest.TestCase):
    def test_status_prints_a_line_per_commit_and_never_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repo(directory)
            base = repo.commit("base", {"README.md": "x"})
            repo.commit("auth change", {"server/zerg/auth/tokens.py": "1"})
            result = repo.run("status", "--range", f"{base}..HEAD")
            self.assertEqual(result.returncode, 0)
            self.assertIn("NOT OK", result.stdout)
            self.assertIn("auth", result.stdout)



class PathsTests(unittest.TestCase):
    """The provider factory's epoch gate: every commit touching a pinned verifier file must be cleanly reviewed."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = Repo(self._dir.name)
        self.base = self.repo.commit("base", {"README.md": "x"})
        self.listed = Path(self._dir.name) / "pinned.txt"
        self.listed.write_text("# pinned\nserver/zerg/qa/oracle.py\n")

    def tearDown(self):
        self._dir.cleanup()

    def run_paths(self):
        result = self.repo.run("paths", "--range", f"{self.base}..HEAD", "--paths-file", str(self.listed), "--json")
        return result.returncode, json.loads(result.stdout)

    def test_only_commits_touching_a_listed_file_are_judged(self):
        self.repo.commit("unrelated", {"server/zerg/other.py": "1"})
        code, out = self.run_paths()
        self.assertEqual((code, out["checked"], out["refused"]), (0, 0, []))

    def test_an_unreviewed_commit_to_a_listed_file_refuses(self):
        sha = self.repo.commit("oracle change", {"server/zerg/qa/oracle.py": "1"})
        code, out = self.run_paths()
        self.assertEqual(code, 1)
        self.assertEqual([r["sha"] for r in out["refused"]], [sha])
        self.assertEqual(out["refused"][0]["files"], ["server/zerg/qa/oracle.py"])
        self.assertIn("no review receipt", out["refused"][0]["reasons"][0])

    def test_an_open_blocking_finding_refuses_until_dispositioned(self):
        self.repo.commit("oracle change", {"server/zerg/qa/oracle.py": "1"})
        rid = self.repo.receipt(self.base, findings=[finding()])
        code, out = self.run_paths()
        self.assertEqual(code, 1)
        self.assertIn(f"unresolved blocking finding {rid} F1", out["refused"][0]["reasons"][0])
        self.repo.disposition(rid, "F1", "rejected")
        self.assertEqual(self.run_paths()[0], 0)

    def test_an_empty_list_is_a_gate_fault_not_a_pass(self):
        self.listed.write_text("# nothing pinned\n\n")
        self.repo.commit("oracle change", {"server/zerg/qa/oracle.py": "1"})
        result = self.repo.run("paths", "--range", f"{self.base}..HEAD", "--paths-file", str(self.listed))
        self.assertEqual(result.returncode, 2)
        self.assertIn("lists no paths", result.stderr)

    def test_paths_never_reads_the_policy(self):
        self.repo.commit("oracle change", {"server/zerg/qa/oracle.py": "1"})
        result = subprocess.run([sys.executable, str(GATE), "--repo", str(self.repo.dir), "--policy",
                                 str(self.repo.dir / "missing.toml"), "paths", "--range", f"{self.base}..HEAD",
                                 "--paths-file", str(self.listed)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_a_listed_test_or_doc_path_is_judged_even_though_the_policy_exempts_it(self):
        self.listed.write_text("docs/oracle.md\n")
        self.repo.commit("doc", {"docs/oracle.md": "1"})
        self.assertEqual(self.run_paths()[0], 1)

if __name__ == "__main__":
    unittest.main()
