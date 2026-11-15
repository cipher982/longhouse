#!/usr/bin/env python3
"""Repair a red main: find the commit that broke it, revert it, tell its author.

Runs from .github/workflows/main-repair.yml when CI or Terminal Truth completes red on main. Control-plane spec
multi-agent-dev-loop-speed.md, change 3: no merge queue; a briefly red main that repairs itself costs less than a
serialized queue behind a 17-minute CI.

It acts only when every guard below holds, and otherwise says why it did nothing:

1. The failing run is on main, from a push, and is the NEWEST non-cancelled run of its workflow on main. A newer
   finished green run that contains the failing commit means someone already fixed forward: stand down. A newer run
   still in progress will report for itself.
2. Flake check: the first time a run fails, rerun its failed jobs once and stop; the rerun's own completion starts
   this script again. Only a run that failed on its second attempt (attempt >= 2) is deterministic.
3. The range is the commits since the newest earlier green run of the same workflow that is an ancestor of the
   failing head. No green ancestor, a merge commit, or more than MAX_RANGE commits: abstain.
4. Attribution by evidence. One commit in range: it. Several: bisect by running the failing check at each probe
   (backend tests by file, validate, engine tests, frontend, managed launch); Terminal Truth cells cannot be probed
   here (they need the stock provider CLIs), so there the culprit is the one commit in range that touches the lane's
   paths, else abstain. The probe must fail at the culprit and pass at its parent.
5. Never revert a revert, never revert a commit carrying `[no-auto-revert]`, never revert once MAX_REVERTS_PER_6H
   bot reverts are already on main, never push when the revert conflicts, and never touch a tag: the only ref this
   script writes is refs/heads/main, with a fast-forward push.

After a revert it writes the marker ref refs/ship-repaired/<culprit>/<revert>. The Sauron job longhouse-ship-watch finds
the culprit's session through the refs/ship/<sha>/<session>/... marker `make ship` left, sends it the revert commit
message (failing run, jobs, log excerpt) and deletes the marker; this script holds no Longhouse credential. A push made
with the workflow's own token starts no push workflows, so the revert is also dispatched to CI, the runtime image,
Deploy and Verify and Terminal Truth.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO = os.environ.get("GITHUB_REPOSITORY", "cipher982/longhouse")
REVALIDATE = (  # workflow file, inputs; a GITHUB_TOKEN push starts nothing, but workflow_dispatch is exempt
    ("contract-first-ci.yml", {}),
    ("runtime-image.yml", {}),
    ("deploy-and-verify.yml", {}),
    ("terminal-truth.yml", {"publish_status": "true"}),
)
HANDLED_WORKFLOWS = ("CI", "Terminal Truth")
FAILED = frozenset({"failure", "timed_out", "startup_failure"})
MAX_RANGE = 30
MAX_REVERTS_PER_6H = 2
NO_REVERT_MARKER = "[no-auto-revert]"
TRAILER = "Main-Repair"
TT_PATHS_FILE = ".github/workflows/terminal-truth.yml"

_FAILED_TEST = re.compile(r"FAILED\s+(tests_lite/[\w/.\-]+\.py)(?:::(\S+))?")
_RUST_FAILED = re.compile(r"test\s+([\w:]+)\s+\.\.\.\s+FAILED")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")
_ERROR_LINE = re.compile(r"(##\[error\]|\bFAILED\b|\bERROR\b|error(\[E\d+\])?:|AssertionError|panicked at|Diff in|Traceback)")


# --------------------------------------------------------------------------------------------------------------------
# Plain data
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    kind: str  # reverted | stood_down | rerun_requested | abstained | dry_run
    reason: str
    culprit: str | None = None
    revert: str | None = None
    run_url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        return f"main-repair: {self.kind}: {self.reason}"


@dataclass
class Probe:
    """How to tell whether a commit has the failure: a shell command that exits non-zero when it does."""

    name: str
    command: str


def parse_tests(logs: list[str]) -> list[str]:
    files: list[str] = []
    for log in logs:
        for match in _FAILED_TEST.finditer(log):
            if match.group(1) not in files:
                files.append(match.group(1))
    return files


def parse_rust_tests(logs: list[str]) -> list[str]:
    names: list[str] = []
    for log in logs:
        for match in _RUST_FAILED.finditer(log):
            if match.group(1) not in names:
                names.append(match.group(1))
    return names


def excerpt(log: str, limit: int = 1800) -> str:
    lines = [_TS.sub("", raw).rstrip() for raw in log.splitlines()]
    lines = [line for line in lines if line]
    hits = [i for i, line in enumerate(lines) if _ERROR_LINE.search(line)]
    if hits:
        keep: list[str] = []
        seen: set[int] = set()
        for i in hits[:12]:
            for j in range(max(0, i - 1), min(len(lines), i + 2)):
                if j not in seen:
                    seen.add(j)
                    keep.append(lines[j])
        text = "\n".join(keep)
    else:
        text = "\n".join(lines[-30:])
    return text[-limit:]


def probes_for(workflow: str, failed_jobs: list[dict[str, Any]], logs: dict[int, str]) -> list[Probe] | None:
    """Probes that reproduce the failed jobs, or None when some failed job cannot be reproduced here."""
    if workflow != "CI":
        return None
    probes: list[Probe] = []
    for job in failed_jobs:
        name = job["name"]
        text = logs.get(job["id"], "")
        failed_steps = " ".join(s["name"] for s in job.get("steps") or [] if s.get("conclusion") in FAILED)
        if name == "Validation":
            probes.append(Probe("validate", "make validate"))
        elif name.startswith("Backend tests shard"):
            files = parse_tests([text])
            if not files:
                return None
            probes.append(Probe("backend", f'make test TEST="{" ".join(files)}"'))
        elif name == "Engine tests":
            if "rustfmt" in failed_steps:
                probes.append(Probe("rustfmt", "cargo fmt --manifest-path engine/Cargo.toml -- --check"))
            else:
                tests = parse_rust_tests([text])
                if not tests:
                    return None
                probes.append(Probe("engine", f'make test-engine-single TEST="{" ".join(tests)}"'))
        elif name.startswith("Frontend"):
            probes.append(Probe("frontend", "make test-frontend-runner"))
        elif name == "Managed launch lifecycle":
            probes.append(Probe("managed-launch", "make test-managed-launch-lifecycle"))
        elif name == "Backend tests":  # the aggregate job only reports its shards
            continue
        else:
            return None
    return probes or None


# --------------------------------------------------------------------------------------------------------------------
# GitHub and git
# --------------------------------------------------------------------------------------------------------------------


class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        self.repo, self.token = repo, token

    def _req(self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None, raw: bool = False) -> Any:
        url = path if path.startswith("http") else f"https://api.github.com/repos/{self.repo}/{path.lstrip('/')}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "longhouse-main-repair"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409, 422) and method != "GET":
                return {"_status": exc.code}
            raise
        if raw:
            return payload.decode("utf-8", "replace")
        return json.loads(payload) if payload else {}

    def get(self, path: str, **params: Any) -> Any:
        return self._req("GET", path, params=params)

    def post(self, path: str, body: Any = None) -> Any:
        return self._req("POST", path, body)

    def log(self, job_id: int) -> str:
        try:
            return self._req("GET", f"actions/jobs/{job_id}/logs", raw=True)
        except urllib.error.URLError:
            return ""


def run_cmd(args: list[str], cwd: Path, *, check: bool = True, env: dict[str, str] | None = None,
            timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout,
                            env={**os.environ, **(env or {})})
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:400]}")
    return result


class Repo:
    def __init__(self, path: Path, remote: str = "origin") -> None:
        self.path, self.remote = path, remote

    def git(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
        return run_cmd(["git", *args], self.path, check=check, env=env).stdout.strip()

    def ok(self, *args: str) -> bool:
        return run_cmd(["git", *args], self.path, check=False).returncode == 0

    def fetch(self) -> None:
        self.git("fetch", "--quiet", "--no-tags", self.remote, "+refs/heads/main:refs/remotes/origin/main")

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def commits(self, base: str, head: str) -> list[str]:
        out = self.git("rev-list", "--reverse", f"{base}..{head}")
        return out.split() if out else []

    def parents(self, sha: str) -> list[str]:
        return self.git("rev-list", "--parents", "-n", "1", sha).split()[1:]

    def message(self, sha: str) -> str:
        return self.git("log", "-1", "--format=%B", sha)

    def subject(self, sha: str) -> str:
        return self.git("log", "-1", "--format=%s", sha)

    def changed(self, sha: str) -> list[str]:
        return [p for p in self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha).splitlines() if p]


def probe_fails_at(repo: Repo, sha: str, probes: list[Probe], runner: str = "") -> bool:
    """True when any probe fails (the failure is present) in a clean checkout of sha."""
    base = Path(tempfile.mkdtemp(prefix="main-repair-probe-"))
    wt = base / "wt"
    try:
        repo.git("worktree", "add", "--detach", str(wt), sha)
        for probe in probes:
            cmd = f"{runner} '{probe.command}'" if runner else probe.command
            result = subprocess.run(["bash", "-c", cmd], cwd=wt, capture_output=True, text=True, timeout=3000)
            print(f"main-repair: probe {probe.name} at {sha[:10]}: exit {result.returncode}", file=sys.stderr)
            if result.returncode:
                return True
        return False
    finally:
        run_cmd(["git", "worktree", "remove", "--force", str(wt)], repo.path, check=False)
        shutil.rmtree(base, ignore_errors=True)


def bisect(commits: list[str], fails: Callable[[str], bool]) -> tuple[str | None, str]:
    """First failing commit in oldest-to-newest order, assuming the last one fails. (culprit, why-not)."""
    if not fails(commits[-1]):
        return None, f"the failure does not reproduce at {commits[-1][:10]}"
    lo, hi = 0, len(commits) - 1  # commits[hi] fails
    while lo < hi:
        mid = (lo + hi) // 2
        if fails(commits[mid]):
            hi = mid
        else:
            lo = mid + 1
    return commits[lo], ""


# --------------------------------------------------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------------------------------------------------


def tt_paths(repo: Repo) -> list[str]:
    """The push ``paths:`` list of the Terminal Truth workflow (no YAML parser needed on the runner)."""
    paths: list[str] = []
    in_paths = False
    indent = 0
    for raw in (repo.path / TT_PATHS_FILE).read_text().splitlines():
        stripped = raw.strip()
        if not in_paths:
            if stripped == "paths:":
                in_paths, indent = True, len(raw) - len(raw.lstrip())
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and len(raw) - len(raw.lstrip()) >= indent:
            paths.append(stripped[2:].split(" #")[0].strip().strip("\"'"))
            continue
        break
    if not paths:
        raise RuntimeError(f"no push paths found in {TT_PATHS_FILE}")
    return paths


def _glob_match(path: str, pattern: str) -> bool:
    regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, path) is not None


def is_bot_revert(message: str) -> bool:
    return f"\n{TRAILER}: " in "\n" + message or message.lstrip().startswith("Revert ")


def repair(
    gh: Any,
    repo: Repo,
    run_id: int,
    *,
    dry_run: bool = False,
    probe_runner: str = "",
    announce: bool = True,
    probe: Callable[[Repo, str, list[Probe]], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    run = gh.get(f"actions/runs/{run_id}")
    name, sha, url = run["name"], run["head_sha"], run["html_url"]
    if name not in HANDLED_WORKFLOWS:
        return Outcome("abstained", f"{name} is not a workflow main-repair handles", run_url=url)
    if run.get("head_branch") != "main" or run.get("event") != "push":
        return Outcome("abstained", f"run {run_id} is not a push to main", run_url=url)
    if run.get("status") != "completed" or run.get("conclusion") not in FAILED:
        return Outcome("stood_down", f"run {run_id} is {run.get('status')}/{run.get('conclusion')}, not red", run_url=url)

    # Guard 1: only the newest non-cancelled run of this workflow on main speaks for main.
    repo.fetch()
    listing = gh.get(f"actions/workflows/{run['workflow_id']}/runs", branch="main", event="push", per_page=50)
    newer = [r for r in listing.get("workflow_runs") or []
             if int(r["run_number"]) > int(run["run_number"]) and r.get("conclusion") != "cancelled"]
    for other in sorted(newer, key=lambda r: int(r["run_number"]), reverse=True):
        if other.get("status") != "completed":
            return Outcome("stood_down", f"newer {name} run {other['html_url']} is still {other['status']}; it reports for itself", run_url=url)
        if other.get("conclusion") == "success" and repo.is_ancestor(sha, other["head_sha"]):
            return Outcome("stood_down", f"newer green {name} run {other['html_url']} contains {sha[:10]}: fixed forward", run_url=url)
        if other.get("conclusion") in FAILED:
            return Outcome("stood_down", f"newer red {name} run {other['html_url']} supersedes this one; it will be handled", run_url=url)

    # Guard 2: flake check. The rerun's completion starts this script again with attempt 2.
    attempt = int(run.get("run_attempt") or 1)
    if attempt < 2:
        if dry_run:
            return Outcome("dry_run", "would rerun the failed jobs once and wait for the rerun to report", run_url=url)
        res = gh.post(f"actions/runs/{run_id}/rerun-failed-jobs")
        if res.get("_status"):
            return Outcome("abstained", f"rerun request refused ({res['_status']}); not acting on an unconfirmed failure", run_url=url)
        return Outcome("rerun_requested", "rerunning the failed jobs once to rule out a flake", run_url=url)

    # Guard 3: the range since the newest earlier green ancestor.
    older = [r for r in listing.get("workflow_runs") or []
             if int(r["run_number"]) < int(run["run_number"]) and r.get("status") == "completed" and r.get("conclusion") == "success"]
    base = next((r["head_sha"] for r in sorted(older, key=lambda r: int(r["run_number"]), reverse=True)
                 if repo.is_ancestor(r["head_sha"], sha)), None)
    if base is None:
        return Outcome("abstained", "no earlier green run of this workflow on main is an ancestor; the range is unknown", run_url=url)
    commits = repo.commits(base, sha)
    if not commits:
        return Outcome("abstained", f"empty range {base[:10]}..{sha[:10]}", run_url=url)
    if len(commits) > MAX_RANGE:
        return Outcome("abstained", f"{len(commits)} commits since the last green run; too many to attribute", run_url=url)
    if any(len(repo.parents(c)) > 1 for c in commits):
        return Outcome("abstained", "the range holds a merge commit; reverting it is a human decision", run_url=url)

    # Guard 5 (part): a loop breaker. Several bot reverts already on main means attribution is not working.
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 6 * 3600))
    recent = repo.git("log", "origin/main", f"--since={since}", f"--grep=^{TRAILER}: revert", "--format=%H").split()
    if len(recent) >= MAX_REVERTS_PER_6H:
        return Outcome("abstained", f"{len(recent)} automatic reverts already landed in the last 6 hours; a human looks next", run_url=url)

    # Guard 4: attribution.
    jobs = gh.get(f"actions/runs/{run_id}/jobs", per_page=100).get("jobs") or []
    failed_jobs = [j for j in jobs if j.get("conclusion") in FAILED]
    logs = {j["id"]: gh.log(j["id"]) for j in failed_jobs[:6]}
    probes = probes_for(name, failed_jobs, logs)
    culprit: str | None = None
    if len(commits) == 1:
        culprit = commits[0]
    elif name == "Terminal Truth":
        paths = tt_paths(repo)
        touching = [c for c in commits if any(_glob_match(f, p) for f in repo.changed(c) for p in paths)]
        if len(touching) != 1:
            return Outcome("abstained", f"{len(touching)} of {len(commits)} commits touch Terminal Truth paths and its cells "
                           "cannot be probed here; attribution needs a human", run_url=url)
        culprit = touching[0]
    elif probes is None:
        return Outcome("abstained", "a failed job cannot be reproduced here, so the culprit among "
                       f"{len(commits)} commits cannot be proven", run_url=url)
    else:
        run_probe = probe or (lambda r, c, p: probe_fails_at(r, c, p, probe_runner))
        culprit, why = bisect(commits, lambda c: run_probe(repo, c, probes))
        if culprit is None:
            return Outcome("abstained", why, run_url=url)
        parent = repo.parents(culprit)[0]
        if parent != base and run_probe(repo, parent, probes):
            return Outcome("abstained", f"the failure is already present at {parent[:10]}, before {culprit[:10]}", run_url=url)

    # Guard 5: what may be reverted.
    message = repo.message(culprit)
    if is_bot_revert(message):
        return Outcome("abstained", f"{culprit[:10]} is itself a revert; main-repair never reverts a revert", culprit=culprit, run_url=url)
    if NO_REVERT_MARKER in message:
        return Outcome("abstained", f"{culprit[:10]} carries {NO_REVERT_MARKER}", culprit=culprit, run_url=url)
    subject = repo.subject(culprit)
    failed_names = ", ".join(sorted({j["name"] for j in failed_jobs})) or name
    evidence = "\n\n".join(excerpt(t) for t in logs.values() if t)[:1500]
    body = (f'Revert "{subject}"\n\nmain-repair: {name} failed deterministically on main at {sha[:12]} ({failed_names}; '
            f"second attempt failed too) and {culprit[:12]} is the first commit that fails it.\nRun: {url}\n"
            f"Re-land it with the fix on top.\n" + (f"\nLog excerpt:\n{evidence}\n" if evidence else "")
            + f"\n{TRAILER}: revert {culprit}\n")
    if dry_run:
        return Outcome("dry_run", f"would revert {culprit[:10]} {subject!r}", culprit=culprit, run_url=url,
                       details={"range": [c[:10] for c in commits], "message": body})

    revert_sha = push_revert(repo, culprit, body)
    if revert_sha is None:
        return Outcome("abstained", f"reverting {culprit[:10]} conflicts with later commits or main moved; a human decides", culprit=culprit, run_url=url)
    if announce:
        gh.post("git/refs", {"ref": f"refs/ship-repaired/{culprit}/{revert_sha}", "sha": revert_sha})
        for workflow, inputs in REVALIDATE:
            res = gh.post(f"actions/workflows/{workflow}/dispatches", {"ref": "main", "inputs": inputs} if inputs else {"ref": "main"})
            if res.get("_status"):
                print(f"main-repair: could not dispatch {workflow} ({res['_status']})", file=sys.stderr)
    return Outcome("reverted", f"reverted {culprit[:10]} {subject!r} as {revert_sha[:10]}", culprit=culprit, revert=revert_sha, run_url=url)


def push_revert(repo: Repo, culprit: str, message: str, attempts: int = 3) -> str | None:
    """Revert culprit on top of origin/main and fast-forward refs/heads/main to it. None when it cannot be done cleanly."""
    for _ in range(attempts):
        repo.fetch()
        tip = repo.git("rev-parse", "origin/main")
        base_commit = repo.parents(culprit)[0]
        # revert = a three-way merge with the culprit as base and its parent as "theirs"
        merged = run_cmd(["git", "merge-tree", "--write-tree", f"--merge-base={culprit}", tip, base_commit], repo.path, check=False)
        if merged.returncode:
            return None
        tree = merged.stdout.strip().splitlines()[0]
        if tree == repo.git("rev-parse", f"{tip}^{{tree}}"):
            return None  # nothing to revert: already undone
        env = {"GIT_AUTHOR_NAME": "longhouse-main-repair", "GIT_AUTHOR_EMAIL": "main-repair@users.noreply.github.com",
               "GIT_COMMITTER_NAME": "longhouse-main-repair", "GIT_COMMITTER_EMAIL": "main-repair@users.noreply.github.com"}
        new = repo.git("commit-tree", tree, "-p", tip, "-m", message, env=env)
        pushed = run_cmd(["git", "push", repo.remote, f"{new}:refs/heads/main"], repo.path, check=False)
        if pushed.returncode == 0:
            return new
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", type=int, required=True, help="the red workflow run to examine")
    parser.add_argument("--repo-dir", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true", help="decide and print; rerun nothing, push nothing, message nobody")
    parser.add_argument("--probe-runner", default="", help="prefix that runs a probe command, e.g. 'scripts/ops/remote.sh run'")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("main-repair: GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    outcome = repair(GitHub(REPO, token), Repo(args.repo_dir), args.run_id, dry_run=args.dry_run,
                     probe_runner=args.probe_runner, announce=not args.dry_run)
    print(outcome.line())
    print(json.dumps({"kind": outcome.kind, "reason": outcome.reason, "culprit": outcome.culprit, "revert": outcome.revert,
                      "run": outcome.run_url, **outcome.details}))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(f"### main-repair\n\n`{outcome.kind}`: {outcome.reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
