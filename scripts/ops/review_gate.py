#!/usr/bin/env python3
"""Review gate: refuse a push or a promotion that carries unreviewed work.

Reviews are off the landing path: a reversible change may land, and reach the
canary, before its review finishes. Two things are not allowed to move on
without one:

  push       commits that touch the blocking list (scripts/ops/review-policy.toml:
             migrations/schema, auth, billing/provisioning, CI and secrets, engine
             storage/shipper/state, binding/liveness/control authority) need a
             completed review receipt before they land.
  promotion  every commit between the currently served SHA and the target that is
             neither docs/tests nor release bookkeeping needs a completed receipt,
             and no receipt covering it may carry an unresolved blocking or
             material finding.

Receipts are written by `hatch review` (review-hub) into the repo's git common
dir, `review-receipts/receipts.jsonl`, and finding dispositions are appended with
`hatch review disposition`. The format is described in
control-plane/docs/specs/review-receipts.md. A receipt covers a commit when it
lists the commit's SHA or its stable patch-id, so a rebase after review keeps the
review; a change to the patch itself (amend, conflict resolution) does not.

A merge commit is judged by what it adds to a clean merge of its parents, not by the
commits it brings in (those are reviewed on their own): its own changes are
`git show --remerge-diff`, the conflict resolutions and any hand-added content. A merge
with none asks for nothing. A merge with some needs a receipt keyed to the merge SHA,
written by `hatch review --merge <sha>`, which reviews exactly that diff.

  review_gate.py push [--base origin/main]
  review_gate.py pre-push REMOTE [URL]        (the git hook; stdin is git's list of refs being pushed)
  review_gate.py promotion --target SHA (--served SHA | --served-url URL) [--start-reviews]
  review_gate.py status [--range A..B]
  review_gate.py backfill --range A..B         (start background reviews for the range's unreviewed commits)
  review_gate.py queue [--wait SECONDS]        (the background reviews: queued, running, done, failed)

Reviews start themselves. A push to main that the pre-push hook lets through queues a background
`hatch review` of the pushed commits that are not exempt and have no completed receipt, and returns
at once; a refused promotion (`--start-reviews`, which the promote scripts pass) queues the commits it
refused for lack of a receipt. The queue (`<git common dir>/review-receipts/auto/`) is deduplicated
by SHA and patch-id, runs at most AUTO_SLOTS reviews at a time, and retries a review that ended
without a completed receipt (a pass hit its budget, or the run failed) once, AUTO_RETRY_DELAY_S later.
Nothing here weakens a gate: the push rule still needs a completed review before a blocking-list
commit lands, and promotion still refuses until every receipt exists and no gated finding is open.

`push` is asked by the scripts that push (make check-push-readiness, make ship, release.sh), which is
cooperative: a bare `git push origin HEAD:main` skipped it, and on 2026-09-29 three commits to the gate
and to promote-production.sh landed with no receipt that way. scripts/ops/install-push-gate.sh installs
`pre-push` as a git hook in the shared git dir, so every worktree's push to main is asked whichever
recipe the agent follows. `git push --no-verify` still bypasses it; the promotion rule is the backstop.

Exit 0: allowed. Exit 1: refused (the message lists the commits). Exit 2: the gate
could not decide (unresolvable SHA, unreachable health URL); promotion treats that
as a refusal.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1
POLICY_RELPATH = "scripts/ops/review-policy.toml"
STORE_DIRNAME = "review-receipts"
LOG_NAME = "receipts.jsonl"
GATED_SEVERITIES = ("blocking", "material")
RESOLVING = ("fixed", "rejected")
DEFAULT_BRANCH = "main"
OVERRIDE_ENV = "LONGHOUSE_REVIEW_OVERRIDE"
OVERRIDE_REASON_ENV = "LONGHOUSE_REVIEW_OVERRIDE_REASON"
OVERRIDE_WHO = "david"


class GateError(Exception):
    """The gate could not decide."""


# --- git -------------------------------------------------------------------

def git(repo: str | Path, *args: str, stdin: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, input=stdin,
                          capture_output=True, text=True, errors="replace")
    if check and proc.returncode != 0:
        raise GateError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def resolve(repo: str | Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False).strip() or _missing(rev)


def _missing(rev: str) -> str:
    raise GateError(f"{rev} is not a commit in this checkout (git fetch first)")


def repo_name(repo: str | Path) -> str:
    url = git(repo, "remote", "get-url", "origin", check=False).strip()
    return re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1])


# --- policy ----------------------------------------------------------------

def compile_glob(pattern: str) -> re.Pattern[str]:
    """`*` stays inside a path segment, `**` crosses segments, `**/` may match nothing
    (the same subset .github/path-filters.yml and scripts/ci/affected.py use)."""
    out, i = ["^"], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


@dataclass
class Policy:
    exempt: list[re.Pattern[str]]
    bump_subject: re.Pattern[str] | None
    bump_files: list[re.Pattern[str]]
    blocking: list[tuple[str, list[re.Pattern[str]]]]  # (area, patterns)
    blocking_globs: list[str] = field(default_factory=list)  # the globs as written, in the same order

    @classmethod
    def load(cls, path: str | Path, name: str) -> "Policy":
        data = tomllib.loads(Path(path).read_text())
        bump = data.get("release_bump", {})
        blocking = [(entry["area"], [compile_glob(p) for p in entry["paths"]])
                    for entry in data.get("repos", {}).get(name, {}).get("blocking", [])]
        return cls(
            exempt=[compile_glob(p) for p in data.get("exempt", {}).get("paths", [])],
            bump_subject=re.compile(bump["subject"]) if bump.get("subject") else None,
            bump_files=[compile_glob(p) for p in bump.get("paths", [])],
            blocking=blocking,
            blocking_globs=[p for entry in data.get("repos", {}).get(name, {}).get("blocking", []) for p in entry["paths"]],
        )

    def dead_globs(self, tracked: list[str]) -> list[str]:
        compiled = [p for _, patterns in self.blocking for p in patterns]
        return [glob for glob, p in zip(self.blocking_globs, compiled) if not any(p.match(f) for f in tracked)]

    def blocking_areas(self, files: list[str]) -> list[str]:
        return [area for area, patterns in self.blocking
                if any(p.match(f) for f in files for p in patterns)]

    def exempt_commit(self, subject: str, files: list[str]) -> bool:
        """Docs/tests only, release version bookkeeping, or no files at all."""
        if all(any(p.match(f) for p in self.exempt) for f in files):
            return True
        return bool(self.bump_subject and self.bump_subject.match(subject)
                    and all(any(p.match(f) for p in self.bump_files) for f in files))


# --- commits in a range ----------------------------------------------------

@dataclass
class Commit:
    sha: str
    subject: str
    files: list[str]  # for a merge: the files of its own changes (merge_own_files), not of the commits it brings in
    merge: bool = False
    _patch_id: str | None = field(default=None, repr=False)

    def patch_id(self, repo: str | Path) -> str | None:
        if self.merge:
            return None  # a merge is covered by its exact SHA only; its patch is not a stable thing to match
        if self._patch_id is None:
            diff = git(repo, "show", "--no-color", "--no-ext-diff", "--no-renames", "--patch", self.sha)
            out = git(repo, "patch-id", "--stable", stdin=diff).split()
            self._patch_id = out[0] if out else ""
        return self._patch_id or None


def merge_own_files(repo: str | Path, sha: str, parents: list[str]) -> list[str]:
    """The files a merge commit changes beyond a clean automatic merge of its parents: its conflict
    resolutions and anything added by hand (`git show --remerge-diff`, git >= 2.36). Empty for a clean merge.

    Not `--cc`: that lists a file whenever the merged result differs from both parents as a whole, which any
    clean merge of two edits to one file does, and it omits a conflict resolved by taking one side wholesale."""
    if len(parents) > 2:
        raise GateError(f"{sha[:12]} is an octopus merge ({len(parents)} parents): git cannot remerge-diff it, so its own "
                        "changes cannot be measured; merge the branches pairwise")
    try:
        out = git(repo, "show", "--remerge-diff", "--no-renames", "--name-only", "--format=", sha)
    except GateError as exc:
        raise GateError(f"cannot compute the own changes of merge {sha[:12]} (needs git >= 2.36 for --remerge-diff): {exc}")
    return [n for n in out.splitlines() if n.strip()]


def commits_in(repo: str | Path, *revs: str) -> list[Commit]:
    # --no-renames: a rename lists both its source and destination, so moving a file out of
    # (or into) a blocking path or docs cannot hide it from the path rules.
    # --diff-merges=off: a merge lists no files here whatever log.diffMerges says; its own changes are
    # asked of git separately (merge_own_files), so a clean merge lists none and asks for nothing.
    out = git(repo, "log", "--diff-merges=off", "--reverse", "--no-renames", "--name-only", "--format=%x01%H%x02%P%x02%s", *revs)
    commits = []
    for chunk in out.split("\x01")[1:]:
        header, _, names = chunk.partition("\n")
        sha, parents, subject = header.split("\x02", 2)
        parent_list = parents.split()
        merge = len(parent_list) > 1
        files = merge_own_files(repo, sha, parent_list) if merge else [n for n in names.splitlines() if n.strip()]
        commits.append(Commit(sha, subject.strip(), files, merge=merge))
    return commits


def grandfather_boundary(repo: str | Path) -> str | None:
    """The parent of the commit that introduced the policy file. Commits from before
    the review lane existed carry no receipts and are not asked for any."""
    added = git(repo, "log", "--diff-filter=A", "--format=%H", "--", POLICY_RELPATH, check=False).split()
    if not added:
        return None
    parents = git(repo, "rev-list", "--parents", "-n", "1", added[-1]).split()
    return parents[1] if len(parents) > 1 else None


# --- receipts --------------------------------------------------------------

def store_dir(repo: str | Path) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()) / STORE_DIRNAME


def load_events(repo: str | Path) -> list[dict]:
    try:
        text = (store_dir(repo) / LOG_NAME).read_text(errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("schema") == SCHEMA and _well_formed(event):
            events.append(event)
    return events


def _well_formed(event: dict) -> bool:
    """Skip events the gate could not index (a hand-edited or half-written line) rather than crash on them."""
    if event.get("type") == "review":
        return (isinstance(event.get("id"), str) and isinstance(event.get("commits"), list)
                and all(isinstance(c, dict) and c.get("sha") for c in event["commits"])
                and all(isinstance(f, dict) and f.get("id") for f in event.get("findings") or []))
    if event.get("type") == "disposition":
        return isinstance(event.get("receipt"), str) and bool(event.get("finding"))
    return True


def append_event(repo: str | Path, event: dict) -> None:
    directory = store_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    line = (json.dumps({"schema": SCHEMA, **event}, separators=(",", ":")) + "\n").encode()
    fd = os.open(directory / LOG_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def open_findings(events: list[dict], receipt: dict) -> list[dict]:
    latest = {}
    for e in events:
        if e.get("type") == "disposition" and e.get("receipt") == receipt["id"]:
            latest[e.get("finding")] = e.get("disposition")
    return [f for f in receipt.get("findings", [])
            if f.get("severity") in GATED_SEVERITIES and latest.get(f.get("id")) not in RESOLVING]


@dataclass
class Verdict:
    commit: Commit
    reasons: list[str]  # empty = fine
    areas: list[str] = field(default_factory=list)
    needs_receipt: bool = False  # a review would clear it (no receipt, or only a partial one)


def check_commits(repo: str | Path, commits: list[Commit], events: list[dict]) -> list[Verdict]:
    """Coverage of each commit by review receipts: needs a complete receipt that lists its
    SHA (or patch-id), and no covering receipt may hold an unresolved gated finding.
    A merge is covered only by a receipt entry flagged `merge` (`hatch review --merge`, which
    reviewed the merge's own changes) under the merge's exact SHA."""
    reviews = [e for e in events if e.get("type") == "review"]
    by_sha: dict[str, list[dict]] = {}
    by_merge: dict[str, list[dict]] = {}
    by_patch: dict[str, list[dict]] = {}
    for r in reviews:
        for c in r.get("commits", []):
            by_sha.setdefault(c.get("sha"), []).append(r)
            if c.get("merge"):
                by_merge.setdefault(c.get("sha"), []).append(r)
            if c.get("patch_id"):
                by_patch.setdefault(c["patch_id"], []).append(r)
    verdicts = []
    for commit in commits:
        covering = list((by_merge if commit.merge else by_sha).get(commit.sha, []))
        if by_patch and not commit.merge and not any(r.get("state") == "complete" for r in covering):
            # Not reviewed under this exact SHA: a rebased copy of the same patch counts.
            seen = {r["id"] for r in covering}
            covering += [r for r in by_patch.get(commit.patch_id(repo) or "", []) if r["id"] not in seen]
        reasons = []
        needs_receipt = False
        complete = [r for r in covering if r.get("state") == "complete"]
        if not complete:
            needs_receipt = True
            if covering:
                latest = covering[-1]
                why = "; ".join(latest.get("state_reasons") or []) or "not complete"
                reasons.append(f"only a partial review ({latest['id']}: {why})")
            elif commit.merge:
                reasons.append("no review receipt for the merge's own changes (conflict resolution or content beyond "
                               "a clean merge of its parents): " + ", ".join(commit.files[:5])
                               + (", ..." if len(commit.files) > 5 else ""))
            else:
                reasons.append("no review receipt")
        for r in covering:
            for f in open_findings(events, r):
                reasons.append(f"unresolved {f['severity']} finding {r['id']} {f['id']}: {f.get('summary', '')[:140]}")
        verdicts.append(Verdict(commit, reasons, needs_receipt=needs_receipt))
    return verdicts


# --- modes -----------------------------------------------------------------

def push_verdicts(repo: str | Path, policy: Policy, base: str, head: str = "HEAD") -> list[Verdict]:
    return revs_verdicts(repo, policy, [f"{resolve(repo, base)}..{resolve(repo, head)}"])


def revs_verdicts(repo: str | Path, policy: Policy, revs: list[str]) -> list[Verdict]:
    commits = commits_in(repo, *revs)
    gated = [(c, policy.blocking_areas(c.files)) for c in commits]
    gated = [(c, areas) for c, areas in gated if areas]
    verdicts = check_commits(repo, [c for c, _ in gated], load_events(repo))
    for v, (_, areas) in zip(verdicts, gated):
        v.areas = areas
    return [v for v in verdicts if v.reasons]


def main_updates(stdin_lines: list[str]) -> list[tuple[str, str]]:
    """Git's pre-push stdin is `<local ref> <local sha> <remote ref> <remote sha>` per ref being pushed.
    Only a push that updates main is asked (as (local sha, remote sha)); a topic branch, a tag or a deletion is not."""
    updates = []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) == 4 and parts[2] == f"refs/heads/{DEFAULT_BRANCH}" and set(parts[1]) != {"0"}:
            updates.append((parts[1], parts[3]))
    return updates


def pre_push_verdicts(repo: str | Path, policy: Policy, updates: list[tuple[str, str]]) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for local_sha, remote_sha in updates:
        known = set(remote_sha) != {"0"} and git(repo, "rev-parse", "--verify", "--quiet", f"{remote_sha}^{{commit}}", check=False).strip()
        head = resolve(repo, local_sha)
        if known:
            revs = [f"{remote_sha}..{head}"]
        else:
            # The remote's main is a commit this checkout never fetched (the push will be refused as a
            # non-fast-forward unless it is forced) or main does not exist there yet. Either way, ask about
            # every commit no remote's main is known to hold (a commit on some topic branch still counts,
            # it is about to reach main), back to the point the review lane began.
            boundary = grandfather_boundary(repo)
            revs = [head, "--not", "--glob=refs/remotes/*/" + DEFAULT_BRANCH, *([boundary] if boundary else [])]
        verdicts += revs_verdicts(repo, policy, revs)
    return verdicts


def check_policy(repo: str | Path, policy: Policy, name: str) -> None:
    """The push rule is only as good as the policy it reads: a dead glob is a protection a rename switched off."""
    dead = policy.dead_globs(git(repo, "ls-files").split("\n"))
    if dead:
        print("review-gate: WARNING these blocking globs match no tracked file (a rename may have switched a "
              "protection off; fix scripts/ops/review-policy.toml): " + ", ".join(dead), file=sys.stderr)
    if not policy.blocking:
        raise GateError(f"review-policy.toml has no [[repos.{name}.blocking]] table, so "
                        "the landing rule would enforce nothing here; add one or pass --name")


def promotion_revs(repo: str | Path, served: str, target: str) -> list[str]:
    revs = [f"{resolve(repo, served)}..{resolve(repo, target)}"]
    boundary = grandfather_boundary(repo)
    if boundary:
        revs.append(f"^{boundary}")
    return revs


def promotion_verdicts(repo: str | Path, policy: Policy, served: str, target: str) -> list[Verdict]:
    revs = promotion_revs(repo, served, target)
    commits = [c for c in commits_in(repo, *revs) if not policy.exempt_commit(c.subject, c.files)]
    verdicts = check_commits(repo, commits, load_events(repo))
    for v in verdicts:
        v.areas = policy.blocking_areas(v.commit.files)
    return [v for v in verdicts if v.reasons]


def served_commit(url: str) -> str:
    try:
        # Cloudflare answers the default Python-urllib agent with 403 (error 1010). A URL without a scheme
        # raises ValueError from Request itself, so the request is built inside the try.
        request = urllib.request.Request(url, headers={"User-Agent": "longhouse-review-gate/1"})
        with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310 - operator-supplied health URL
            body = json.load(resp)
    except (OSError, ValueError) as exc:
        raise GateError(f"could not read the served commit from {url}: {exc}")
    sha = ((body.get("build") or {}).get("commit") or "").strip() if isinstance(body, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GateError(f"{url} does not report a full build.commit (got {sha!r})")
    return sha


# --- automatic background reviews ------------------------------------------
#
# A job is one `hatch review` invocation: a contiguous run of commits (`--base first^ --head last`) or one
# merge's own changes (`--merge`). jobs.json holds every job, guarded by an flock on queue.lock; a job is
# queued, running (with the worker's pid), done (every commit has a completed receipt) or failed (it still
# lacked one after AUTO_MAX_ATTEMPTS runs; a later push, backfill or promotion queues those commits again).

AUTO_DIRNAME = "auto"
AUTO_SLOTS = 2               # reviews running at once on this machine
AUTO_MAX_ATTEMPTS = 2        # the first run and one retry
AUTO_RETRY_DELAY_S = 300
AUTO_RUN_TIMEOUT_S = 1900    # Hatch's own hard budget is 30 min; a review normally takes 3-5
AUTO_MAX_RUN = 8             # commits per range review: a longer diff overruns the reviewer's budget
AUTO_KEEP_S = 7 * 86400      # finished jobs and their run logs are pruned after this
AUTO_LOG_MAX_BYTES = 2_000_000
HATCH_ENV = "LONGHOUSE_AUTOREVIEW_HATCH"  # the hatch binary to use; "off" disables automatic reviews
SESSION_ENV = ("LONGHOUSE_MANAGED_SESSION_ID", "LONGHOUSE_SESSION_ID", "LONGHOUSE_CHANNEL_SESSION_ID")  # as review-hub reads them
# Set by git for the hook that runs us; a review of another checkout must not inherit them.
GIT_HOOK_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH")


def auto_dir(repo: str | Path) -> Path:
    return store_dir(repo) / AUTO_DIRNAME


def auto_log_path(repo: str | Path) -> Path:
    return auto_dir(repo) / "autoreview.log"


def primary_checkout(repo: str | Path) -> str:
    """Reviews run against the clone's primary checkout: the worktree that pushed is often removed right after."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return str(common.parent) if common.name == ".git" and (common.parent / ".git").exists() else str(Path(repo).resolve())


def hatch_binary() -> str | None:
    configured = os.environ.get(HATCH_ENV, "").strip()
    if configured.lower() == "off":
        return None
    return configured or shutil.which("hatch")


def _now() -> float:
    return time.time()


def _stamp(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else _now()))


def auto_log(repo: str | Path, message: str) -> None:
    path = auto_log_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > AUTO_LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
    with open(path, "a") as fh:
        fh.write(f"{_stamp()} [{os.getpid()}] {message}\n")


class _QueueLock:
    def __init__(self, repo: str | Path):
        self.dir = auto_dir(repo)

    def __enter__(self) -> "_QueueLock":
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.dir / "queue.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        os.close(self.fd)


def _load_jobs(repo: str | Path) -> list[dict]:
    try:
        jobs = json.loads((auto_dir(repo) / "jobs.json").read_text())
    except (OSError, ValueError):
        return []
    return [j for j in jobs if isinstance(j, dict) and j.get("id")] if isinstance(jobs, list) else []


def _save_jobs(repo: str | Path, jobs: list[dict]) -> None:
    path = auto_dir(repo) / "jobs.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=1))
    tmp.replace(path)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _settle(repo: str | Path, jobs: list[dict]) -> list[dict]:
    """Requeue (or fail) jobs whose worker died, and prune finished jobs past AUTO_KEEP_S."""
    kept = []
    for job in jobs:
        if job.get("state") == "running" and not _alive(job.get("pid")):
            job["state"] = "queued" if job.get("attempts", 0) < AUTO_MAX_ATTEMPTS else "failed"
            job["note"] = "its worker died"
            job["updated"] = _now()
        if job.get("state") in ("done", "failed") and _now() - job.get("updated", 0) > AUTO_KEEP_S:
            (auto_dir(repo) / "runs" / f"{job['id']}.log").unlink(missing_ok=True)
            continue
        kept.append(job)
    return kept


def needed_commits(repo: str | Path, policy: Policy, revs: list[str]) -> list[tuple[Commit, bool]]:
    """Every commit of the range in order, each with whether it needs a review: not exempt and no completed receipt."""
    commits = commits_in(repo, *revs)
    gated = [c for c in commits if not policy.exempt_commit(c.subject, c.files)]
    missing = {v.commit.sha for v in check_commits(repo, gated, load_events(repo)) if v.needs_receipt}
    return [(c, c.sha in missing) for c in commits]


def plan_jobs(repo: str | Path, ordered: list[tuple[Commit, bool]]) -> list[dict]:
    """Group the commits that need a review into contiguous runs of at most AUTO_MAX_RUN; a merge is its own job."""
    jobs, run = [], []

    def flush():
        if run:
            parents = git(repo, "rev-list", "--parents", "-n", "1", run[0].sha).split()
            if len(parents) > 1:  # a root commit has no base to review against
                jobs.append({"kind": "range", "base": parents[1], "head": run[-1].sha, "commits": [c.sha for c in run],
                             "patch_ids": [c.patch_id(repo) for c in run]})
            run.clear()

    for commit, needed in ordered:
        if not needed:
            flush()
        elif commit.merge:
            flush()
            jobs.append({"kind": "merge", "base": None, "head": commit.sha, "commits": [commit.sha], "patch_ids": []})
        else:
            run.append(commit)
            if len(run) >= AUTO_MAX_RUN:
                flush()
    flush()
    return jobs


def enqueue_reviews(repo: str | Path, policy: Policy, revs: list[str], *, session: str | None, reason: str) -> list[dict]:
    """Queue background reviews for the range's commits that need one and no queued or running job already
    covers (by SHA or patch-id, so a rebased re-push does not queue the same work twice); start a worker.
    Returns the new jobs. Never raises for a review that cannot start: this runs on the push path."""
    hatch = hatch_binary()
    if not hatch:
        return []
    ordered = needed_commits(repo, policy, revs)
    if not any(needed for _, needed in ordered):
        return []
    with _QueueLock(repo):
        jobs = _settle(repo, _load_jobs(repo))
        active = [j for j in jobs if j.get("state") in ("queued", "running")]
        busy_shas = {s for j in active for s in j.get("commits", [])}
        busy_patches = {p for j in active for p in j.get("patch_ids", []) if p}
        ordered = [(c, needed and c.sha not in busy_shas and (c.merge or c.patch_id(repo) not in busy_patches))
                   for c, needed in ordered]
        new = plan_jobs(repo, ordered)
        for i, job in enumerate(new):
            job.update(id=f"ar-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{job['head'][:7]}-{os.getpid()}-{i}",
                       state="queued", attempts=0, not_before=0, pid=None, session=session, reason=reason,
                       created=_now(), updated=_now(), history=[])
        jobs += new
        _save_jobs(repo, jobs)
        queued = sum(1 for j in jobs if j.get("state") == "queued")
    for job in new:
        span = f"--merge {job['head'][:12]}" if job["kind"] == "merge" else f"{job['base'][:12]}..{job['head'][:12]}"
        auto_log(repo, f"queued {job['id']} ({reason}): {span}, {len(job['commits'])} commit(s), "
                       f"intent={'longhouse:' + session if session else 'none'}")
    if queued:
        # Also when nothing is new: a job requeued after its worker died needs a worker, and a spare one
        # finds every slot taken and exits at once.
        start_workers(repo, min(AUTO_SLOTS, queued))
    return new


def start_workers(repo: str | Path, count: int) -> None:
    """Detached workers: no stdout/stderr tie to the caller (the pre-push hook captures ours), own session."""
    primary = primary_checkout(repo)
    env = {k: v for k, v in os.environ.items() if k not in GIT_HOOK_ENV}
    log = open(auto_log_path(repo), "a")
    try:
        for _ in range(count):
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--repo", primary, "autoreview-worker"],
                             cwd=primary, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
    finally:
        log.close()


def _take_slot(repo: str | Path) -> int | None:
    for i in range(AUTO_SLOTS):
        fd = os.open(auto_dir(repo) / f"slot-{i}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            os.close(fd)
    return None


def _job_commits(repo: str | Path, job: dict) -> list[Commit]:
    return [c for sha in job["commits"] for c in commits_in(repo, f"{sha}^!")]


def run_job(repo: str | Path, job: dict, hatch: str) -> dict:
    """One `hatch review` of the job; returns the attempt record. The launcher builds every input the reviewer
    sees (arm's-length): this passes references only, never prose."""
    cmd = [hatch, "review", "-C", str(repo)]
    cmd += ["--merge", job["head"]] if job["kind"] == "merge" else ["--base", job["base"], "--head", job["head"]]
    errored_before = any(h.get("exit") not in (0, None) and not h.get("receipt") for h in job.get("history", []))
    cmd += ["--session", job["session"]] if job.get("session") and not errored_before else ["--no-intent"]
    runs = auto_dir(repo) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    started = _now()
    with open(runs / f"{job['id']}.log", "a") as out:
        out.write(f"\n### {_stamp()} attempt {job['attempts']}: {' '.join(cmd)}\n")
        out.flush()
        proc = subprocess.Popen(cmd, cwd=str(repo), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        try:
            code = proc.wait(timeout=AUTO_RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.terminate()  # review-hub stops its reviewer and the reviewer's processes on SIGTERM
            try:
                code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
    covering = [e for e in load_events(repo) if e.get("type") == "review"
                and e.get("at", "") >= _stamp(started - 1) and {c.get("sha") for c in e.get("commits", [])} & set(job["commits"])]
    return {"at": _stamp(), "exit": code, "elapsed_s": round(_now() - started),
            "receipt": covering[-1]["id"] if covering else None, "receipt_state": covering[-1].get("state") if covering else None}


def autoreview_worker(repo: str | Path) -> int:
    """Run queued jobs until none is left. At most AUTO_SLOTS workers hold a slot; a worker that finds both
    taken exits at once (the workers that hold them drain the queue, new jobs included). A worker releases
    its slot only while holding the queue lock, so a job queued after it looked is seen by the next worker."""
    hatch = hatch_binary()
    if not hatch:
        return 0
    auto_dir(repo).mkdir(parents=True, exist_ok=True)
    slot = _take_slot(repo)
    if slot is None:
        return 0
    try:
        while True:
            with _QueueLock(repo):
                jobs = _settle(repo, _load_jobs(repo))
                queued = [j for j in jobs if j.get("state") == "queued"]
                ready = [j for j in queued if j.get("not_before", 0) <= _now()]
                if not queued:
                    _save_jobs(repo, jobs)
                    os.close(slot)
                    slot = None
                    return 0
                job = ready[0] if ready else None
                if job:
                    job.update(state="running", pid=os.getpid(), attempts=job.get("attempts", 0) + 1, updated=_now())
                _save_jobs(repo, jobs)
            if not job:
                time.sleep(max(1, min(60, min(j.get("not_before", 0) for j in queued) - _now())))
                continue
            auto_log(repo, f"running {job['id']} attempt {job['attempts']}")
            try:
                attempt = run_job(repo, job, hatch)
            except Exception as exc:  # noqa: BLE001 - one bad job must not strand the rest of the queue
                attempt = {"at": _stamp(), "exit": None, "error": f"{type(exc).__name__}: {exc}", "receipt": None}
            try:
                covered = not any(v.needs_receipt for v in check_commits(repo, _job_commits(repo, job), load_events(repo)))
            except GateError as exc:
                covered, attempt["error"] = False, str(exc)
            with _QueueLock(repo):
                jobs = _load_jobs(repo)
                current = next((j for j in jobs if j["id"] == job["id"]), job)
                current.setdefault("history", []).append(attempt)
                current["pid"] = None
                current["updated"] = _now()
                if covered:
                    current["state"] = "done"
                elif current.get("attempts", 0) < AUTO_MAX_ATTEMPTS:
                    current.update(state="queued", not_before=_now() + AUTO_RETRY_DELAY_S)
                else:
                    current["state"] = "failed"
                if current is job:
                    jobs.append(job)
                _save_jobs(repo, jobs)
            auto_log(repo, f"{current['state']} {job['id']}: exit={attempt.get('exit')} receipt={attempt.get('receipt')} "
                           f"({attempt.get('receipt_state') or attempt.get('error') or 'no receipt'})"
                           + (f"; retry in {AUTO_RETRY_DELAY_S}s" if current["state"] == "queued" else ""))
    finally:
        if slot is not None:
            os.close(slot)


def describe_queue(repo: str | Path) -> tuple[list[str], list[dict]]:
    with _QueueLock(repo):
        jobs = _settle(repo, _load_jobs(repo))
        _save_jobs(repo, jobs)
    lines = []
    for j in jobs:
        span = f"--merge {j['head'][:12]}" if j.get("kind") == "merge" else f"{(j.get('base') or '')[:12]}..{j['head'][:12]}"
        last = (j.get("history") or [{}])[-1]
        tail = f" last: exit={last.get('exit')} receipt={last.get('receipt')} {last.get('receipt_state') or ''}".rstrip() if last else ""
        lines.append(f"{j['id']} {j.get('state'):7} {span} {len(j.get('commits', []))} commit(s) attempts={j.get('attempts', 0)}"
                     f"{' pid=' + str(j['pid']) if j.get('pid') else ''}{tail}")
    return lines, jobs


def queued_note(repo: str | Path, jobs: list[dict]) -> str:
    commits = sum(len(j["commits"]) for j in jobs)
    return (f"review-gate: started {len(jobs)} background review(s) of {commits} unreviewed commit(s) "
            f"(at most {AUTO_SLOTS} at a time, ~5 min each); log {auto_log_path(repo)}")


def start_reviews_quietly(repo: str | Path, policy: Policy, revs: list[str], reason: str,
                          session: str | None = None) -> list[dict]:
    """Queue reviews without ever turning a queue fault into a gate verdict: the gate decides on receipts alone."""
    try:
        return enqueue_reviews(repo, policy, revs, session=session, reason=reason)
    except Exception as exc:  # noqa: BLE001
        print(f"review-gate: could not start background reviews ({type(exc).__name__}: {exc}); "
              f"run hatch review yourself (log {auto_log_path(repo)})", file=sys.stderr)
        return []


def pushed_review_note(repo: str | Path, policy: Policy, updates: list[tuple[str, str]]) -> None:
    """After an allowed push to main: queue reviews of the pushed commits that need one, print one line, return."""
    session = next((os.environ[n].strip() for n in SESSION_ENV if os.environ.get(n, "").strip()), None)
    for local_sha, remote_sha in updates:
        if set(remote_sha) == {"0"} or not git(repo, "rev-parse", "--verify", "--quiet", f"{remote_sha}^{{commit}}", check=False).strip():
            continue  # an unknown remote main: the push will be refused as a non-fast-forward anyway
        jobs = start_reviews_quietly(repo, policy, [f"{remote_sha}..{local_sha}"], "push", session)
        if jobs:
            print(queued_note(repo, jobs), file=sys.stderr)


def queue_mode(repo: str, wait: int | None) -> int:
    deadline = _now() + (wait or 0)
    _, jobs = describe_queue(repo)
    queued = sum(1 for j in jobs if j.get("state") == "queued")
    if queued and hatch_binary():
        start_workers(repo, min(AUTO_SLOTS, queued))  # a job whose worker died is never stranded
    while True:
        lines, jobs = describe_queue(repo)
        active = [j for j in jobs if j.get("state") in ("queued", "running")]
        if not wait or not active or _now() >= deadline:
            break
        time.sleep(15)
    print("\n".join(lines) or "review-gate: no background reviews.")
    print(f"log: {auto_log_path(repo)}  (hatch output per review: {auto_dir(repo) / 'runs'}/<id>.log)")
    if wait is None:
        return 0
    if active:
        print(f"review-gate: {len(active)} review(s) still queued or running after {wait}s", file=sys.stderr)
        return 1
    return 1 if any(j.get("state") == "failed" for j in jobs) else 0


# --- reporting -------------------------------------------------------------

def refusal(repo: str, kind: str, what: str, verdicts: list[Verdict], started: list[dict] | None = None) -> str:
    lines = [f"review-gate: REFUSED {kind}: {len(verdicts)} commit(s) {what}", ""]
    for v in verdicts:
        area = f" [{', '.join(v.areas)}]" if v.areas else ""
        lines.append(f"  {v.commit.sha[:12]} {v.commit.subject[:80]}{area}")
        lines.extend(f"      - {reason}" for reason in v.reasons)
    unreviewed = [v.commit.sha for v in verdicts if v.needs_receipt and not v.commit.merge]
    merges = [v.commit.sha for v in verdicts if v.needs_receipt and v.commit.merge]
    lines.append("")
    if started is not None:
        queued = {sha for j in started for sha in j["commits"]}
        if started:
            note = queued_note(repo, started).removeprefix("review-gate: ")
            lines += [note[:1].upper() + note[1:] + ".",
                      f"Wait for them, then retry: python3 {Path(__file__).resolve()} --repo {repo} queue --wait 1800",
                      "A commit refused for an open finding needs its disposition, not another review."]
        else:
            lines.append("No new background review was started: every unreviewed commit is already queued or running "
                         f"(python3 {Path(__file__).resolve()} --repo {repo} queue), or hatch is not on PATH.")
        unreviewed = [sha for sha in unreviewed if sha not in queued]
        merges = [sha for sha in merges if sha not in queued]
    if unreviewed:
        first, last = unreviewed[0], unreviewed[-1]
        lines += [
            "Get a receipt (reviews an exact commit range in a throwaway checkout; --no-intent for someone else's commits):",
            f"  hatch review -C {repo} --base {first[:12]}^ --head {last[:12]} --no-intent",
            "A rebase after review keeps the receipt. A partial review (a pass hit its budget) does not count.",
        ]
    if merges:
        lines.append("A merge is covered by a review of its own changes, the diff against a clean merge of its parents "
                     "(`git show --remerge-diff`), keyed to the merge SHA; a range review does not cover it:")
        lines += [f"  hatch review -C {repo} --merge {sha[:12]} --no-intent" for sha in merges]
    lines += [
        "Then record what you did about each blocking/material finding:",
        '  hatch review disposition <receipt> F1[,F2] fixed|rejected --reason "..."   # `hatch review receipts --open` lists them',
        "Reviews live in this repo's git common dir (review-receipts/), shared by every worktree.",
        f"David only: {OVERRIDE_ENV}={OVERRIDE_WHO} {OVERRIDE_REASON_ENV}='why' overrides this check and is logged.",
    ]
    return "\n".join(lines)


def override(repo: str | Path, kind: str, target: str, verdicts: list[Verdict]) -> bool:
    if os.environ.get(OVERRIDE_ENV, "").strip().lower() != OVERRIDE_WHO:
        return False
    reason = os.environ.get(OVERRIDE_REASON_ENV, "").strip()
    if not reason:
        print(f"review-gate: {OVERRIDE_ENV} is set but {OVERRIDE_REASON_ENV} is empty; the override needs a reason.", file=sys.stderr)
        return False
    append_event(repo, {"type": "override", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "by": OVERRIDE_WHO, "gate": kind, "target": target, "reason": reason,
                        "commits": [{"sha": v.commit.sha, "reasons": v.reasons} for v in verdicts]})
    print(f"review-gate: OVERRIDDEN by {OVERRIDE_WHO} ({reason}); {len(verdicts)} commit(s) waved through, logged.", file=sys.stderr)
    return True


# --- cli -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".", help="repository (default: cwd)")
    parser.add_argument("--policy", help=f"policy file (default: {POLICY_RELPATH} of the longhouse checkout this script is in)")
    parser.add_argument("--name", help="policy table for this repository (default: origin URL basename)")
    sub = parser.add_subparsers(dest="mode", required=True)
    push = sub.add_parser("push", help="landing rule: blocking-list commits need a completed review")
    push.add_argument("--base", default="origin/main")
    push.add_argument("--head", default="HEAD")
    hook = sub.add_parser("pre-push", help="git pre-push hook: the landing rule for whatever `git push` is about to send to main")
    hook.add_argument("remote")
    hook.add_argument("url", nargs="?")
    promo = sub.add_parser("promotion", help="promotion rule: every code commit since the served SHA is reviewed")
    promo.add_argument("--target", required=True)
    group = promo.add_mutually_exclusive_group(required=True)
    group.add_argument("--served")
    group.add_argument("--served-url")
    promo.add_argument("--start-reviews", action="store_true",
                       help="on a refusal, queue background reviews of the commits that lack a receipt")
    status = sub.add_parser("status", help="print the verdict of every commit of a range (never refuses)")
    status.add_argument("--range", dest="rng", default="origin/main..HEAD")
    backfill = sub.add_parser("backfill", help="queue background reviews of a range's commits that lack a receipt")
    backfill.add_argument("--range", dest="rng", required=True)
    queue = sub.add_parser("queue", help="list the background reviews (starting a worker for any queued one); with --wait, "
                           "non-zero when it times out or a review failed")
    queue.add_argument("--wait", type=int, metavar="SECONDS", help="block until no review is queued or running")
    sub.add_parser("autoreview-worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    repo = str(Path(args.repo).resolve())
    if args.mode == "autoreview-worker":
        return autoreview_worker(repo)
    if args.mode == "queue":
        return queue_mode(repo, args.wait)
    policy_path = args.policy or str(Path(__file__).resolve().parent / "review-policy.toml")
    started = None
    try:
        try:
            policy = Policy.load(policy_path, args.name or repo_name(repo))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # A missing or malformed policy is a gate fault, not a refusal: exit 1 means "refused".
            raise GateError(f"cannot read the review policy {policy_path}: {type(exc).__name__}: {exc}")
        if args.mode == "push":
            if not git(repo, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}", check=False).strip():
                print(f"review-gate: {args.base} not found; push check skipped.", file=sys.stderr)
                return 0
            check_policy(repo, policy, args.name or repo_name(repo))
            verdicts = push_verdicts(repo, policy, args.base, args.head)
            kind, what, target = "push", "touch the blocking list without a completed review", args.head
        elif args.mode == "pre-push":
            updates = main_updates(sys.stdin.read().splitlines())
            if not updates:
                return 0  # a topic branch, a tag or a deletion: nothing to ask, nothing to print
            check_policy(repo, policy, args.name or repo_name(repo))
            verdicts = pre_push_verdicts(repo, policy, updates)
            kind, what, target = "push", "touch the blocking list without a completed review", DEFAULT_BRANCH
            if not verdicts:
                pushed_review_note(repo, policy, updates)
        elif args.mode == "promotion":
            served = args.served or served_commit(args.served_url)
            verdicts = promotion_verdicts(repo, policy, served, args.target)
            kind, what, target = "promotion", f"in {served[:12]}..{args.target[:12]} lack a completed review or hold unresolved findings", args.target
            if verdicts and args.start_reviews and not os.environ.get(OVERRIDE_ENV):
                started = start_reviews_quietly(repo, policy, promotion_revs(repo, served, args.target), "promotion")
        elif args.mode == "backfill":
            jobs = enqueue_reviews(repo, policy, [args.rng], session=None, reason="backfill")
            print(queued_note(repo, jobs) if jobs else "review-gate: nothing to start (every commit is exempt, reviewed, "
                  "queued or running, or hatch is not on PATH).")
            return 0
        else:
            base, _, head = args.rng.partition("..")
            for c in commits_in(repo, args.rng):
                v = check_commits(repo, [c], load_events(repo))[0]
                areas = policy.blocking_areas(c.files)
                exempt = policy.exempt_commit(c.subject, c.files)
                label = "exempt (clean merge)" if exempt and c.merge and not c.files else "exempt" if exempt \
                    else "reviewed" if not v.reasons else "NOT OK "
                # No gate asks about an exempt commit, so its coverage reasons would only be noise.
                print(f"{c.sha[:12]} {label} {','.join(areas) or '-'} {c.subject[:60]} "
                      f"{'' if exempt else '; '.join(v.reasons)}".rstrip())
            return 0
    except GateError as exc:
        print(f"review-gate: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - an uncaught exception exits 1, which callers read as "refused"
        print(f"review-gate: internal error, could not decide: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not verdicts:
        if args.mode != "pre-push":  # a hook that prints on every push is a hook people stop reading
            print(f"review-gate: {kind} OK.")
        return 0
    if override(repo, kind, target, verdicts):
        return 0
    print(refusal(repo, kind, what, verdicts, started), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
