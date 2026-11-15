#!/usr/bin/env python3
"""scripts/ci/main_repair.py against a real temporary git repository and a fake GitHub."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("main_repair", ROOT / "scripts" / "ci" / "main_repair.py")
assert SPEC and SPEC.loader
mr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mr
SPEC.loader.exec_module(mr)

TT_WORKFLOW = "on:\n  push:\n    paths:\n      - \"server/zerg/**\"\n"


def sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class World:
    """origin (bare) + clone; commits land on main with one marker file each."""

    def __init__(self, tmp: str) -> None:
        self.tmp = Path(tmp)
        self.origin = self.tmp / "origin.git"
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.work = self.tmp / "work"
        sh(self.tmp, "git", "clone", "-q", str(self.origin), str(self.work))
        for k, v in (("user.email", "a@example.com"), ("user.name", "a")):
            sh(self.work, "git", "config", k, v)
        (self.work / ".github/workflows").mkdir(parents=True)
        (self.work / ".github/workflows/terminal-truth.yml").write_text(TT_WORKFLOW)
        self.commit("base", {"state.txt": "ok\n"})
        sh(self.work, "git", "tag", "v0.0.1")
        sh(self.work, "git", "push", "-q", "origin", "main", "--tags")
        self.base = sh(self.work, "git", "rev-parse", "HEAD")

    def commit(self, subject: str, files: dict[str, str], body: str = "") -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        sh(self.work, "git", "add", "-A")
        sh(self.work, "git", "commit", "-q", "-m", subject + (f"\n\n{body}" if body else ""))
        return sh(self.work, "git", "rev-parse", "HEAD")

    def push(self) -> None:
        sh(self.work, "git", "push", "-q", "origin", "main")

    def origin_head(self) -> str:
        return sh(self.origin, "git", "rev-parse", "main")

    def origin_tags(self) -> str:
        return sh(self.origin, "git", "tag", "--list")


class FakeGh:
    def __init__(self, world: World, head: str, *, attempt: int = 2, name: str = "CI", others: list[dict] | None = None,
                 jobs: list[dict] | None = None, ship_refs: list[str] | None = None, log: str = "") -> None:
        self.run = {"id": 100, "name": name, "head_sha": head, "head_branch": "main", "event": "push", "status": "completed",
                    "conclusion": "failure", "html_url": "https://gh/run/100", "workflow_id": 7, "run_number": 50,
                    "run_attempt": attempt}
        green = {"id": 90, "name": name, "head_sha": world.base, "status": "completed", "conclusion": "success",
                 "html_url": "https://gh/run/90", "run_number": 40, "workflow_id": 7}
        self.listing = [self.run, green] + (others or [])
        self.jobs = jobs if jobs is not None else [{"id": 1, "name": "Validation", "conclusion": "failure", "steps": []}]
        self.ship_refs = ship_refs or []
        self.log_text = log
        self.reruns: list[int] = []
        self.posts: list[str] = []

    def get(self, path: str, **params: Any) -> Any:
        if path == "actions/runs/100":
            return self.run
        if path.startswith("actions/workflows/"):
            return {"workflow_runs": self.listing}
        if path.endswith("/jobs"):
            return {"jobs": self.jobs}
        if path.startswith("git/matching-refs/ship/"):
            return [{"ref": r} for r in self.ship_refs]
        raise AssertionError(path)

    def post(self, path: str, body: Any = None) -> Any:
        if path.endswith("rerun-failed-jobs"):
            self.reruns.append(int(path.split("/")[2]))
        else:
            self.posts.append(path + (" " + body["ref"] if path == "git/refs" else ""))
        return {}

    def log(self, job_id: int) -> str:
        return self.log_text


def run_repair(world: World, gh: FakeGh, *, broken_from: str | None = None, dry: bool = False) -> tuple[Any, list[str], list[str]]:
    sent = gh.posts
    probed: list[str] = []
    repo = mr.Repo(world.work)

    def probe(r: Any, sha: str, probes: list[Any]) -> bool:
        probed.append(sha)
        return r.git("show", f"{sha}:state.txt") == "broken"

    outcome = mr.repair(gh, repo, 100, dry_run=dry, announce=not dry, probe=probe)
    return outcome, sent, probed


def broken_world(tmp: str, n_after: int = 0, conflict: bool = False) -> tuple[World, list[str]]:
    w = World(tmp)
    c1 = w.commit("feat one", {"a.txt": "1\n"})
    c2 = w.commit("break it", {"state.txt": "broken\n", "b.txt": "2\n"})
    c3 = w.commit("feat three", {"c.txt": "3\n"})
    shas = [c1, c2, c3]
    for i in range(n_after):
        shas.append(w.commit(f"later {i}", {"state.txt": "broken\n" if conflict else "broken\n", f"later{i}.txt": "x\n"}))
    w.push()
    return w, shas


def test_bisect_finds_the_culprit_reverts_it_wakes_its_session_and_touches_no_tag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        gh = FakeGh(w, shas[-1], log="2026-11-15T01:00:00.1Z E   AssertionError: boom\n",
                    jobs=[{"id": 1, "name": "Validation", "conclusion": "failure", "steps": []}])
        tags = w.origin_tags()
        outcome, sent, probed = run_repair(w, gh)
        assert outcome.kind == "reverted", outcome
        assert outcome.culprit == shas[1]
        assert len(probed) <= 4
        assert w.origin_head() == outcome.revert
        sh(w.work, "git", "pull", "-q")
        assert (w.work / "state.txt").read_text() == "ok\n"
        assert (w.work / "a.txt").exists() and (w.work / "c.txt").exists(), "later good commits stay"
        message = sh(w.work, "git", "log", "-1", "--format=%B")
        assert "Main-Repair: revert " + shas[1] in message and "AssertionError: boom" in message and "https://gh/run/100" in message
        assert sent[0] == f"git/refs refs/ship-repaired/{shas[1]}/{outcome.revert}", sent
        assert {p.split("/")[-2] for p in sent[1:]} == {"contract-first-ci.yml", "runtime-image.yml", "deploy-and-verify.yml", "terminal-truth.yml"}
        assert w.origin_tags() == tags


def test_first_failure_only_reruns_once_and_changes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        gh = FakeGh(w, shas[-1], attempt=1)
        head = w.origin_head()
        outcome, sent, probed = run_repair(w, gh)
        assert outcome.kind == "rerun_requested" and gh.reruns == [100]
        assert w.origin_head() == head and probed == [] and sent == []


def test_a_newer_green_run_covering_the_commit_stands_down() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        fix = w.commit("fix forward", {"state.txt": "ok\n"})
        w.push()
        newer = {"id": 101, "name": "CI", "head_sha": fix, "status": "completed", "conclusion": "success",
                 "html_url": "https://gh/run/101", "run_number": 51, "workflow_id": 7}
        gh = FakeGh(w, shas[-1], others=[newer])
        head = w.origin_head()
        outcome, sent, probed = run_repair(w, gh)
        assert outcome.kind == "stood_down" and "fixed forward" in outcome.reason
        assert w.origin_head() == head and probed == [] and sent == []


def test_a_newer_run_in_progress_or_red_stands_down() -> None:
    for status, conclusion in (("in_progress", None), ("completed", "failure")):
        with tempfile.TemporaryDirectory() as tmp:
            w, shas = broken_world(tmp)
            newer = {"id": 101, "name": "CI", "head_sha": shas[-1], "status": status, "conclusion": conclusion,
                     "html_url": "https://gh/run/101", "run_number": 51, "workflow_id": 7}
            outcome, *_ = run_repair(w, FakeGh(w, shas[-1], others=[newer]))
            assert outcome.kind == "stood_down", outcome


def test_a_cancelled_newer_run_does_not_hide_a_red_main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        newer = {"id": 101, "name": "CI", "head_sha": shas[-1], "status": "completed", "conclusion": "cancelled",
                 "html_url": "https://gh/run/101", "run_number": 51, "workflow_id": 7}
        outcome, *_ = run_repair(w, FakeGh(w, shas[-1], others=[newer]))
        assert outcome.kind == "reverted", outcome


def test_never_reverts_a_revert_or_a_marked_commit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        bad = w.commit('Revert "something"', {"state.txt": "broken\n"})
        w.push()
        head = w.origin_head()
        outcome, *_ = run_repair(w, FakeGh(w, bad))
        assert outcome.kind == "abstained" and "never reverts a revert" in outcome.reason
        assert w.origin_head() == head
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        bad = w.commit("risky", {"state.txt": "broken\n"}, body="[no-auto-revert]")
        w.push()
        outcome, *_ = run_repair(w, FakeGh(w, bad))
        assert outcome.kind == "abstained" and "no-auto-revert" in outcome.reason


def test_a_revert_that_conflicts_with_later_work_is_left_to_a_human() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        w.commit("good", {"x.txt": "1\n"})
        bad = w.commit("break it", {"state.txt": "broken\n"})
        w.commit("builds on the break", {"state.txt": "broken and extended\n"})
        w.push()
        head = w.origin_head()
        gh = FakeGh(w, head)

        def probe(r: Any, sha: str, probes: list[Any]) -> bool:
            return r.git("show", f"{sha}:state.txt").startswith("broken")

        out = mr.repair(gh, mr.Repo(w.work), 100, probe=probe)
        assert out.kind == "abstained" and out.culprit == bad and "conflicts" in out.reason
        assert w.origin_head() == head


def test_failure_that_does_not_reproduce_or_predates_the_culprit_is_not_reverted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        head = w.origin_head()
        out = mr.repair(FakeGh(w, shas[-1]), mr.Repo(w.work), 100, probe=lambda r, s, p: False)
        assert out.kind == "abstained" and "does not reproduce" in out.reason
        assert w.origin_head() == head
        # fails everywhere in the range, including the first commit's parent being the green base: culprit is c1, no earlier failure
        out = mr.repair(FakeGh(w, shas[-1]), mr.Repo(w.work), 100, probe=lambda r, s, p: s != w.base)
        assert out.culprit == shas[0]


def test_unreproducible_jobs_in_a_multi_commit_range_abstain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        gh = FakeGh(w, shas[-1], jobs=[{"id": 2, "name": "UI popover sweep (desktop)", "conclusion": "failure", "steps": []}])
        out, _, probed = run_repair(w, gh)
        assert out.kind == "abstained" and "cannot be reproduced" in out.reason and probed == []


def test_terminal_truth_needs_exactly_one_commit_touching_its_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        w.commit("docs", {"docs/a.md": "x\n"})
        w.commit("server one", {"server/zerg/a.py": "1\n"})
        w.commit("server two", {"server/zerg/b.py": "2\n"})
        w.push()
        head = w.origin_head()
        out = mr.repair(FakeGh(w, head, name="Terminal Truth"), mr.Repo(w.work), 100)
        assert out.kind == "abstained" and "2 of 3" in out.reason
        assert w.origin_head() == head
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        w.commit("docs", {"docs/a.md": "x\n"})
        only = w.commit("server one", {"server/zerg/a.py": "1\n"})
        w.push()
        out = mr.repair(FakeGh(w, w.origin_head(), name="Terminal Truth"), mr.Repo(w.work), 100, dry_run=True)
        assert out.kind == "dry_run" and out.culprit == only


def test_a_loop_breaker_stops_after_two_recent_automatic_reverts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        w.commit("auto one", {"a.txt": "1\n"}, body="Main-Repair: revert " + "1" * 40)
        w.commit("auto two", {"b.txt": "1\n"}, body="Main-Repair: revert " + "2" * 40)
        bad = w.commit("break it", {"state.txt": "broken\n"})
        w.push()
        out = mr.repair(FakeGh(w, bad), mr.Repo(w.work), 100)
        assert out.kind == "abstained" and "already landed" in out.reason


def test_dry_run_pushes_and_messages_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        head = w.origin_head()
        out, sent, _ = run_repair(w, FakeGh(w, shas[-1], attempt=2), dry=True)
        assert out.kind == "dry_run" and out.culprit == shas[1]
        assert w.origin_head() == head and sent == []
        out, *_ = run_repair(w, FakeGh(w, shas[-1], attempt=1), dry=True)
        assert out.kind == "dry_run" and "rerun" in out.reason


def test_only_ci_and_terminal_truth_on_main_pushes_are_handled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        gh = FakeGh(w, shas[-1], name="Deploy and Verify")
        assert mr.repair(gh, mr.Repo(w.work), 100).kind == "abstained"
        gh = FakeGh(w, shas[-1])
        gh.run["head_branch"] = "topic"
        assert mr.repair(gh, mr.Repo(w.work), 100).kind == "abstained"
        gh = FakeGh(w, shas[-1])
        gh.run["conclusion"] = "success"
        assert mr.repair(gh, mr.Repo(w.work), 100).kind == "stood_down"


# Real lines from the failed ci-VM runs https://github.com/cipher982/longhouse/actions/runs/43760730494 (docs-only commit
# 5484c3a82), .../43735505356 and .../43668365434, plus the TLS handshake line from cube-ci-runner-pools.md.
REAL_INFRA_LOGS = {
    "the runner ran out of disk space": [
        "2026-11-15T21:14:12.2645451Z Error response from daemon: failed to create prepare snapshot dir: failed to create temp dir: "
        "mkdir /var/lib/docker/containerd/daemon/io.containerd.snapshotter.v1.overlayfs/snapshots/new-250879412: no space left on device",
        "2026-11-15T21:14:11.6110024Z OSError: [Errno 28] No space left on device: '/work/.build/locks'",
        "2026-11-15T02:44:43.8995626Z write /var/lib/docker/containerd/daemon/io.containerd.metadata.v1.bolt/meta.db: no space left on device",
    ],
    "the runner was lost": [
        "The self-hosted runner: ci-fast-5swdc-runner-tr2wh lost communication with the server. Verify the machine is running and has a healthy network connection.",
        "The runner has received a shutdown signal. This can happen when the runner service is stopped, or a manually started runner is canceled.",
    ],
    "a network or TLS handshake timeout": [
        "2026-09-30T12:00:00.0Z net/http: TLS handshake timeout",
        "2026-11-14T09:00:00.0Z dial tcp 140.82.112.3:443: i/o timeout",
    ],
    "a GitHub API rate limit": [
        "gh: API rate limit exceeded for installation ID 1234. If you reach out to GitHub Support for help, please include the request ID (HTTP 403)",
        "You have exceeded a secondary rate limit. Please wait a few minutes before you try again.",
    ],
}


def test_real_infrastructure_log_lines_are_classified() -> None:
    for reason, lines in REAL_INFRA_LOGS.items():
        for line in lines:
            assert mr.infrastructure_failure({1: "make: ok\n" + line + "\nmake: *** Error 1"}) == reason, line
    for ordinary in ("E   AssertionError: boom", "FAILED tests_lite/test_a.py::test_x - assert 1 == 2",
                     "error[E0308]: mismatched types", "Diff in engine/src/lib.rs", ""):
        assert mr.infrastructure_failure({1: ordinary}) is None, ordinary


def test_infrastructure_failure_reruns_and_never_reverts_even_for_one_docs_commit() -> None:
    """The 2026-11-15 case: a docs-only commit, red on every attempt for lack of disk, must not be reverted."""
    for reason, lines in REAL_INFRA_LOGS.items():
        with tempfile.TemporaryDirectory() as tmp:
            w = World(tmp)
            docs = w.commit("docs only", {"docs/x.md": "hello\n"})
            w.push()
            gh = FakeGh(w, docs, attempt=2, log=lines[0] + "\n",
                        jobs=[{"id": 1, "name": "Validation", "conclusion": "failure", "steps": []}])
            head = w.origin_head()
            outcome, sent, probed = run_repair(w, gh)
            assert outcome.kind == "rerun_requested" and reason in outcome.reason, outcome
            assert gh.reruns == [100] and "not a code failure" in outcome.reason
            assert w.origin_head() == head and probed == [] and sent == []


def test_infrastructure_failure_in_one_of_many_jobs_stops_the_revert_too() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        jobs = [{"id": 1, "name": "Validation", "conclusion": "failure", "steps": []},
                {"id": 2, "name": "Small checks", "conclusion": "failure", "steps": []}]
        gh = FakeGh(w, shas[-1], attempt=3, jobs=jobs)
        gh.log = lambda job_id: REAL_INFRA_LOGS["the runner ran out of disk space"][2] if job_id == 2 else "AssertionError: boom"
        head = w.origin_head()
        outcome, sent, probed = run_repair(w, gh)
        assert outcome.kind == "rerun_requested" and w.origin_head() == head and probed == []


def test_infrastructure_failure_past_the_attempt_cap_abstains_without_reverting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        gh = FakeGh(w, shas[-1], attempt=mr.MAX_INFRA_ATTEMPTS, log=REAL_INFRA_LOGS["the runner was lost"][0])
        head = w.origin_head()
        outcome, sent, probed = run_repair(w, gh)
        assert outcome.kind == "abstained" and "infrastructure" in outcome.reason and gh.reruns == []
        assert w.origin_head() == head and probed == [] and sent == []


def test_infrastructure_failure_dry_run_changes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        w, shas = broken_world(tmp)
        gh = FakeGh(w, shas[-1], attempt=2, log=REAL_INFRA_LOGS["the runner ran out of disk space"][0])
        out, *_ = run_repair(w, gh, dry=True)
        assert out.kind == "dry_run" and "infrastructure" in out.reason and gh.reruns == []


def test_log_parsing() -> None:
    log = "FAILED tests_lite/test_a.py::test_x - AssertionError\nFAILED tests_lite/test_a.py::test_y\nFAILED tests_lite/test_b.py"
    assert mr.parse_tests([log]) == ["tests_lite/test_a.py", "tests_lite/test_b.py"]
    assert mr.parse_rust_tests(["test omp_print::tests::x ... FAILED"]) == ["omp_print::tests::x"]
    probes = mr.probes_for("CI", [{"id": 1, "name": "Backend tests shard 0/3", "steps": []}], {1: log})
    assert probes and "tests_lite/test_a.py tests_lite/test_b.py" in probes[0].command
    assert mr.probes_for("CI", [{"id": 1, "name": "Backend tests shard 0/3", "steps": []}], {1: "no ids"}) is None
    assert mr.probes_for("Terminal Truth", [], {}) is None


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok {t.__name__}")
    print(f"main-repair tests passed ({len(tests)} scenarios)")
