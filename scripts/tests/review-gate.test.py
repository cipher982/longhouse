#!/usr/bin/env python3
"""The review gate: push rule, promotion rule, receipts, dispositions and the override."""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "ops" / "review_gate.py"
POLICY = ROOT / "scripts" / "ops" / "review-policy.toml"

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
        for sha in self.git("rev-list", "--reverse", f"{base}..{head}").split():
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
        self.assertLess(production.index("lh_review_gate_promotion"), production.index("lh_hosted_prepare_control_plane_auth\n"))

    def test_the_enforcement_path_is_itself_blocking(self):
        policy = gate.Policy.load(POLICY, "longhouse")
        for path in ("scripts/ops/ship.sh", "scripts/ops/release.sh", "scripts/ops/check-push-readiness.sh",
                     "scripts/ops/promote-dogfood.sh", "scripts/ops/promote-production.sh",
                     "scripts/lib/review-gate.sh", "scripts/ops/review_gate.py", "scripts/ops/review-policy.toml"):
            self.assertTrue(policy.blocking_areas([path]), f"{path} must be on the blocking list")


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


if __name__ == "__main__":
    unittest.main()
