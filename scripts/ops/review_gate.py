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
  review_gate.py promotion --target SHA (--served SHA | --served-url URL) [--start-reviews | --attested]
  review_gate.py blocking --base A --head B    (commits touching the blocking list; the dogfood fast lane's guard)
  review_gate.py attest [--target REV ...]     (post the promotion verdict as a GitHub status; see "attestations")
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


def git_ok(repo: str | Path, *args: str) -> bool:
    """A git question answered by its exit status (merge-base --is-ancestor)."""
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True).returncode == 0


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


def remote_main_shas(repo: str | Path) -> list[str]:
    """The main this repo lands on (PUSH_READINESS_REMOTE, default origin), never a fork's or a stale mirror's."""
    remote = os.environ.get("PUSH_READINESS_REMOTE", "").strip() or "origin"
    return git(repo, "for-each-ref", "--format=%(objectname)", f"refs/remotes/{remote}/{DEFAULT_BRANCH}", check=False).split()


class Landed:
    """The history a verdict is about: `tips` (the target, plus every remote main) down to `excludes`
    (the range's lower bound). A receipt speaks for that history when its head landed in it: the head
    is an ancestor of a tip, or the same patch as a commit of the range (a rebase after the review).
    Anything else reviewed a proposal that was discarded or did not land as reviewed (see
    Receipts.counts for what such a receipt may still say)."""

    def __init__(self, repo: str | Path, tips: list[str], excludes: list[str], targets: list[str] | None = None):
        self.repo = repo
        self.tips = list(dict.fromkeys(t for t in tips if t))
        self.excludes = [e for e in excludes if e]
        # The history being judged (the range's own positive ends), without the remote mains a receipt's
        # head may have landed on later: a fix only counts for a target that contains it.
        self.targets = list(dict.fromkeys(t for t in (targets if targets is not None else tips) if t))
        self._shas: set[str] | None = None
        self._patches: set[str] | None = None
        self._target_patches: set[str] | None = None
        self._memo: dict[tuple[str, str | None], bool] = {}
        self._target_memo: dict[tuple[str, str | None], bool] = {}

    @classmethod
    def for_range(cls, repo: str | Path, base: str | None, head: str) -> "Landed":
        target = resolve(repo, head)
        return cls(repo, [target, *remote_main_shas(repo)], [base] if base else [], [target])

    @classmethod
    def for_revs(cls, repo: str | Path, revs: list[str]) -> "Landed":
        """From the rev-list arguments that name a range (`A..B`, `^X`, `B --not X --glob=...`): the range's
        positive ends are tips, its negative ends excludes; every remote main is a tip too, so a receipt whose
        head landed after the target (a review of a longer stretch of main) still speaks for it."""
        tips, excludes, negate = [], [], False
        for rev in revs:
            if rev == "--not":
                negate = True
            elif rev.startswith("--") and not rev.startswith("--glob="):
                raise GateError(f"cannot read a landed history from {rev!r} (A..B, A...B, ^X, --not and --glob= only)")
            elif "..." in rev:
                a, b = (resolve(repo, x or "HEAD") for x in rev.split("...", 1))
                tips += [a, b]
                excludes += git(repo, "merge-base", "--all", a, b, check=False).split()
            elif ".." in rev:
                a, b = rev.split("..", 1)
                excludes.append(a or "HEAD")
                tips.append(resolve(repo, b or "HEAD"))
            elif rev.startswith("^"):
                excludes.append(rev[1:])
            elif negate:
                excludes.append(rev)
            elif rev.startswith("--glob="):
                tips += git(repo, "for-each-ref", "--format=%(objectname)", rev.removeprefix("--glob="), check=False).split()
            else:
                tips.append(resolve(repo, rev))
        return cls(repo, [*tips, *remote_main_shas(repo)], excludes, tips)

    def _range_args(self) -> list[str]:
        return [*self.tips, *(["--not", *self.excludes] if self.excludes else [])]

    def shas(self) -> set[str]:
        if self._shas is None:
            self._shas = set(git(self.repo, "rev-list", *self._range_args(), check=False).split())
        return self._shas

    def _patch_ids(self, args: list[str]) -> set[str]:
        # Same diff options as Commit.patch_id, so a patch-id matches whichever way it was computed.
        log = git(self.repo, "log", "--no-merges", "--no-color", "--no-ext-diff", "--no-renames", "-p", *args, check=False)
        out = git(self.repo, "patch-id", "--stable", stdin=log, check=False) if log.strip() else ""
        return {line.split()[0] for line in out.splitlines() if line.split()}

    def patches(self) -> set[str]:
        """Stable patch-ids of the range's commits, computed once and only when a head is not found by SHA."""
        if self._patches is None:
            self._patches = self._patch_ids(self._range_args())
        return self._patches

    def in_targets(self, sha: str | None, patch_id: str | None) -> bool:
        """Whether a commit (by SHA, or by patch after a rebase) is in the judged targets' own history."""
        key = (sha or "", patch_id)
        if key not in self._target_memo:
            found = bool(sha) and any(git_ok(self.repo, "merge-base", "--is-ancestor", sha, t) for t in self.targets)
            if not found and patch_id and self.targets:
                if self._target_patches is None:
                    self._target_patches = self._patch_ids(
                        [*self.targets, *(["--not", *self.excludes] if self.excludes else [])])
                found = patch_id in self._target_patches
            self._target_memo[key] = found
        return self._target_memo[key]

    def holds(self, sha: str, patch_id: str | None) -> bool:
        key = (sha, patch_id)
        if key not in self._memo:
            self._memo[key] = bool(
                sha in self.shas()
                or any(git_ok(self.repo, "merge-base", "--is-ancestor", sha, tip) for tip in self.tips)
                or (patch_id and patch_id in self.patches()))
        return self._memo[key]


def _default_landed(repo: str | Path, commits: list[Commit]) -> Landed:
    """For a caller that names only commits: their own history down to the first one's parents."""
    first = commits[0].sha if commits else None
    parents = git(repo, "rev-list", "--parents", "-n", "1", first).split()[1:] if first else []
    return Landed(repo, [*(c.sha for c in commits), *remote_main_shas(repo)], parents, [c.sha for c in commits])


def _head_patch(receipt: dict) -> str | None:
    entries = receipt.get("commits") or []
    head = next((c for c in entries if c.get("sha") == receipt.get("head")), None)
    if head is None and entries and not receipt.get("head"):
        head = entries[-1]
    return head.get("patch_id") if head else None


def _keys(entries: list[dict]) -> set[str]:
    return {k for c in entries for k in (c.get("sha"), c.get("patch_id")) if k}


def finding_path(where: str | None) -> str | None:
    """`where` is `path:line[-line]` as the reviewer writes it; the path is everything before the first colon."""
    token = (where or "").strip().strip("`").split(":", 1)[0].strip().strip("`")
    return token or None


class Receipts:
    """The review receipts as one target's history sees them (see Landed), with each gated finding
    attributed to the commits it is about.

    Attribution: a finding names a location (`where`). It is about the commits of its receipt that
    changed that file; when it names no file a reviewed commit changed (or the commit cannot be read),
    it is about every commit of its receipt, never about the rest of a promotion range.

    A receipt whose head did not land (a proposal: withdrawn, reworked, or landed only in part) still
    covers the commits of it that did land, by SHA or patch, but only while it holds no open gated
    finding. One that does has no say at all, coverage and findings both (David, 2026-10-07: an
    unlanded proposal must never affect a target): its finding may be about the part that never
    landed, and attribution by file cannot tell (a revert touches its original's file). A landed
    commit such a receipt covered then needs another covering receipt, and that receipt alone decides:
    a proposal's finding about a landed commit is dropped by design, where the old rule refused it.

    Supersession: an open finding stops counting for a commit once a later complete review covers the
    commit, has no open finding of its own about it, and read a fix: a commit the first review did not
    see that changes the file the finding names (or, for a finding attributed to its whole receipt, a
    head past everything the first review saw). A re-review of the same commits, or of the same commits
    plus unrelated ones, supersedes nothing; that needs a disposition."""

    def __init__(self, repo: str | Path, events: list[dict], landed: Landed):
        self.repo = repo
        self.events = events
        self.landed = landed
        self.reviews = [e for e in events if e.get("type") == "review"]
        self.order = {r["id"]: i for i, r in enumerate(self.reviews)}
        self.by_sha: dict[str, list[dict]] = {}
        self.by_merge: dict[str, list[dict]] = {}
        self.by_patch: dict[str, list[dict]] = {}
        for r in self.reviews:
            for c in r.get("commits", []):
                self.by_sha.setdefault(c.get("sha"), []).append(r)
                if c.get("merge"):
                    self.by_merge.setdefault(c.get("sha"), []).append(r)
                if c.get("patch_id"):
                    self.by_patch.setdefault(c["patch_id"], []).append(r)
        self._relevant: dict[str, bool] = {}
        self._open: dict[str, list[dict]] = {}
        self._about: dict[tuple[str, str], set[str]] = {}
        self._files: dict[str, list[str] | None] = {}

    def relevant(self, r: dict) -> bool:
        if r["id"] not in self._relevant:
            head = r.get("head") or ((r.get("commits") or [{}])[-1].get("sha"))
            self._relevant[r["id"]] = bool(head) and self.landed.holds(head, _head_patch(r))
        return self._relevant[r["id"]]

    def counts(self, r: dict) -> bool:
        return self.relevant(r) or not self.open(r)

    def covering(self, commit: Commit) -> list[dict]:
        covering = [r for r in (self.by_merge if commit.merge else self.by_sha).get(commit.sha, []) if self.counts(r)]
        if self.by_patch and not commit.merge and not any(r.get("state") == "complete" for r in covering):
            # Not reviewed under this exact SHA: a rebased copy of the same patch counts.
            seen = {r["id"] for r in covering}
            covering += [r for r in self.by_patch.get(commit.patch_id(self.repo) or "", [])
                         if r["id"] not in seen and self.counts(r)]
        return sorted(covering, key=lambda r: self.order[r["id"]])

    def open(self, r: dict) -> list[dict]:
        if r["id"] not in self._open:
            self._open[r["id"]] = open_findings(self.events, r)
        return self._open[r["id"]]

    def _entry_files(self, r: dict, entry: dict) -> list[str] | None:
        sha = entry.get("sha")
        key = f"{r['id']}:{sha}" if entry.get("merge") else sha  # a merge's files come from its own receipt
        if key not in self._files:
            files = None
            try:
                if entry.get("merge"):
                    parents = git(self.repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
                    files = list(r.get("merge_files") or merge_own_files(self.repo, sha, parents))
                else:
                    out = git(self.repo, "show", "--no-renames", "--name-only", "--format=", sha, check=False)
                    files = [n for n in out.splitlines() if n.strip()] if out.strip() else None
            except GateError:
                files = None
            self._files[key] = files
        return self._files[key]

    def about(self, r: dict, finding: dict) -> set[str]:
        """The keys (SHAs and patch-ids) of the commits a finding is about."""
        key = (r["id"], str(finding.get("id")))
        if key not in self._about:
            entries = r.get("commits") or []
            path = finding_path(finding.get("where"))
            touched = [c for c in entries if path and path in (self._entry_files(r, c) or [])] if path else []
            self._about[key] = _keys(touched or entries)
        return self._about[key]

    def is_about(self, r: dict, finding: dict, commit: Commit) -> bool:
        keys = self.about(r, finding)
        return commit.sha in keys or (not commit.merge and (commit.patch_id(self.repo) or "") in keys)

    def fixes(self, old: dict, finding: dict, later: dict) -> list[dict]:
        """The commits by which `later` reviewed a fix of `old`'s finding: those `old` did not see that change
        the finding's file, or, when the finding names no file `old`'s commits changed, `later`'s head when it
        is past all of them. Empty when `later` read no fix."""
        seen = _keys(old.get("commits") or [])
        new = [c for c in later.get("commits") or [] if not _keys([c]) & seen]
        path = finding_path(finding.get("where"))
        if path and any(path in (self._entry_files(old, c) or []) for c in old.get("commits") or []):
            return [c for c in new if path in (self._entry_files(later, c) or [])]
        if not new or ({later.get("head"), _head_patch(later)} - {None}) & seen:
            return []
        head = next((c for c in new if c.get("sha") == later.get("head")), new[-1])
        return [head]

    def reads_fix(self, old: dict, finding: dict, later: dict) -> bool:
        """Whether `later` read a fix of the finding that the judged target contains. A review whose head
        landed on main after the target may have read a fix the target does not have; that supersedes nothing."""
        return any(self.landed.in_targets(c.get("sha"), c.get("patch_id")) for c in self.fixes(old, finding, later))

    def blocking(self, commit: Commit, covering: list[dict]) -> list[tuple[dict, dict]]:
        """(receipt, finding) pairs still open about this commit and not superseded."""
        mine = {r["id"]: [f for f in self.open(r) if self.is_about(r, f, commit)] for r in covering}
        out = []
        for r in covering:
            # Only a review whose head landed can vouch for a fix: a clean proposal's "fix" may never have landed.
            later_clean = [r2 for r2 in covering if self.order[r2["id"]] > self.order[r["id"]] and self.relevant(r2)
                           and r2.get("state") == "complete" and not mine[r2["id"]]]
            out += [(r, f) for f in mine[r["id"]] if not any(self.reads_fix(r, f, r2) for r2 in later_clean)]
        return out


@dataclass
class Verdict:
    commit: Commit
    reasons: list[str]  # empty = fine
    areas: list[str] = field(default_factory=list)
    needs_receipt: bool = False  # a review would clear it (no receipt, or only a partial one)


def check_commits(repo: str | Path, commits: list[Commit], events: list[dict], landed: Landed | None = None) -> list[Verdict]:
    """Coverage of each commit by review receipts: needs a complete receipt that lists its
    SHA (or patch-id), and no covering receipt may hold an unresolved gated finding about it.
    A receipt whose head did not land in `landed` (the target's history) counts only while it
    holds no open finding; see Landed and Receipts. A merge is covered only by a receipt entry flagged `merge` (`hatch review --merge`,
    which reviewed the merge's own changes) under the merge's exact SHA."""
    receipts = Receipts(repo, events, landed or _default_landed(repo, commits))
    verdicts = []
    for commit in commits:
        covering = receipts.covering(commit)
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
        for r, f in receipts.blocking(commit, covering):
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
    verdicts = check_commits(repo, [c for c, _ in gated], load_events(repo), Landed.for_revs(repo, revs))
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
    verdicts = check_commits(repo, commits, load_events(repo), Landed.for_revs(repo, revs))
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
    missing = {v.commit.sha for v in check_commits(repo, gated, load_events(repo), Landed.for_revs(repo, revs))
               if v.needs_receipt}
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


def _job_needs_review(repo: str | Path, job: dict) -> bool:
    landed = Landed.for_range(repo, job.get("base"), job["head"])
    return any(v.needs_receipt for v in check_commits(repo, _job_commits(repo, job), load_events(repo), landed))


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
            try:
                # A job can outlive its need (someone reviewed the range by hand, or it sat stranded): skip it.
                already = not _job_needs_review(repo, job)
            except GateError:
                already = False
            if already:
                attempt = {"at": _stamp(), "exit": None, "receipt": None, "note": "already covered; not run"}
            else:
                auto_log(repo, f"running {job['id']} attempt {job['attempts']}")
                try:
                    attempt = run_job(repo, job, hatch)
                except Exception as exc:  # noqa: BLE001 - one bad job must not strand the rest of the queue
                    attempt = {"at": _stamp(), "exit": None, "error": f"{type(exc).__name__}: {exc}", "receipt": None}
            try:
                covered = not _job_needs_review(repo, job)
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


# --- attestations: the promotion verdict, published for promoters that cannot read the receipts -----------
#
# Receipts live in this machine's git common dir; the ring promoter runs on a CI runner (Promote Rings,
# .github/workflows/promote-rings.yml) so production follows dogfood while this machine sleeps. The attester
# (`attest`, run every two minutes by a launchd agent that install-push-gate.sh installs) computes the
# promotion verdict here, from what production serves to each candidate, and posts it as a GitHub commit
# status on the candidate: state plus `from <served sha>: <summary>`, no findings text (the repo is public).
# `promotion --attested` reads it back: it allows the target only when the newest attestation by an allowed
# account is `success` and was computed from a commit the ring's served SHA contains (a range inside a
# clean range is clean). A missing, failed or unreadable attestation refuses, like a missing receipt.

ATTEST_CONTEXT = "longhouse/review-gate"
ATTEST_RE = re.compile(r"^from ([0-9a-f]{40}): ")
ATTESTERS_ENV = "LONGHOUSE_REVIEW_ATTESTERS"  # comma-separated GitHub logins whose attestations count
DEFAULT_ATTESTERS = "cipher982"
PRODUCTION_HEALTH_URL = "https://longhouse.ai/api/health"
DOGFOOD_HEALTH_URL = "https://david010.longhouse.ai/api/health"
ATTEST_REPOST_S = 6 * 3600  # repost an unchanged verdict this often, so a lost post heals itself


def github_slug(repo: str | Path) -> str:
    if os.environ.get("GITHUB_REPOSITORY", "").strip():
        return os.environ["GITHUB_REPOSITORY"].strip()
    url = git(repo, "remote", "get-url", "origin", check=False).strip()
    match = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not match:
        raise GateError(f"cannot tell the GitHub repository from origin {url!r} (set GITHUB_REPOSITORY)")
    return match.group(1)


def is_ancestor(repo: str | Path, older: str, newer: str) -> bool:
    return git_ok(repo, "merge-base", "--is-ancestor", older, newer)


def blocking_commits(repo: str | Path, policy: Policy, base: str, head: str) -> list[tuple[Commit, list[str]]]:
    """The commits of base..head that touch the blocking list, receipts or not (the dogfood fast lane's guard)."""
    found = []
    for commit in commits_in(repo, f"{resolve(repo, base)}..{resolve(repo, head)}"):
        areas = policy.blocking_areas(commit.files)
        if areas:
            found.append((commit, areas))
    return found


def attestation_summary(verdicts: list[Verdict]) -> tuple[str, str]:
    if not verdicts:
        return "success", "clean"
    unreviewed = sum(1 for v in verdicts if v.needs_receipt)
    parts = [f"{unreviewed} unreviewed"] if unreviewed else []
    if len(verdicts) - unreviewed:
        parts.append(f"{len(verdicts) - unreviewed} with open findings")
    return "failure", f"{len(verdicts)} commit(s) refused ({', '.join(parts)}), first {verdicts[0].commit.sha[:12]}"


def _github_get(path: str) -> object:
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "longhouse-review-gate/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
        with urllib.request.urlopen(request, timeout=20) as resp:  # noqa: S310 - fixed GitHub API host
            return json.load(resp)
    except (OSError, ValueError) as exc:
        raise GateError(f"cannot read GitHub {path}: {exc}")


def read_attestation(slug: str, sha: str) -> dict | None:
    """The newest review-gate status on sha posted by an allowed account, or None."""
    allowed = {s.strip().lower() for s in (os.environ.get(ATTESTERS_ENV) or DEFAULT_ATTESTERS).split(",") if s.strip()}
    statuses = _github_get(f"/repos/{slug}/commits/{sha}/statuses?per_page=100")
    if not isinstance(statuses, list):
        raise GateError(f"GitHub returned no status list for {sha[:12]}")
    mine = [s for s in statuses if isinstance(s, dict) and s.get("context") == ATTEST_CONTEXT
            and str((s.get("creator") or {}).get("login") or "").lower() in allowed]
    return max(mine, key=lambda s: str(s.get("created_at") or ""), default=None)


def attested_refusal(repo: str | Path, slug: str, served: str, target: str) -> str | None:
    """None when an attestation allows served..target; otherwise why not."""
    if served != target and not is_ancestor(repo, served, target):
        return (f"{target[:12]} does not contain the served {served[:12]}, so no attestation of a range ending at "
                f"{target[:12]} speaks for it")
    status = read_attestation(slug, target)
    if status is None:
        return (f"no review attestation on {target[:12]} yet (the review attester posts `{ATTEST_CONTEXT}` "
                "from the machine that holds the receipts, every two minutes while it is awake)")
    description = str(status.get("description") or "")
    match = ATTEST_RE.match(description)
    if not match:
        return f"the review attestation on {target[:12]} is malformed: {description!r}"
    if status.get("state") != "success":
        return f"the review attestation on {target[:12]} is {status.get('state')}: {description}"
    base = match.group(1)
    if not git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).strip():
        raise GateError(f"the attestation's base {base[:12]} is not in this checkout (fetch first)")
    if not is_ancestor(repo, base, served):
        return (f"the review attestation on {target[:12]} was computed from {base[:12]}, which the served "
                f"{served[:12]} does not contain; it says nothing about {served[:12]}..{target[:12]}")
    return None


def newest_published(repo: str | Path, slug: str) -> str | None:
    """The newest main commit with a published runtime image: the commit the ring promoter moves dogfood to.
    Highest run number whose commit main holds (the listing's order is not reliable; ring_promoter.py agrees)."""
    try:
        proc = subprocess.run(["gh", "api", f"repos/{slug}/actions/workflows/runtime-image.yml/runs?branch={DEFAULT_BRANCH}"
                               "&status=success&per_page=30", "-q",
                               '.workflow_runs[] | select(.event == "push" or .event == "workflow_dispatch") | "\\(.run_number) \\(.head_sha)"'],
                              capture_output=True, text=True)
    except OSError as exc:  # no gh: attest the other targets
        print(f"review-gate: attest: newest published image unknown: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"review-gate: attest: newest published image unknown: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return None
    runs = sorted((line.split() for line in proc.stdout.splitlines() if len(line.split()) == 2),
                  key=lambda r: int(r[0]) if r[0].isdigit() else 0, reverse=True)
    main = f"refs/remotes/origin/{DEFAULT_BRANCH}"
    return next((sha for _, sha in runs if re.fullmatch(r"[0-9a-f]{40}", sha) and is_ancestor(repo, sha, main)), None)


def post_attestation(slug: str, sha: str, state: str, description: str) -> None:
    proc = subprocess.run(["gh", "api", "--method", "POST", f"repos/{slug}/statuses/{sha}", "-f", f"state={state}",
                           "-f", f"context={ATTEST_CONTEXT}", "-f", f"description={description[:140]}"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise GateError(f"could not post the attestation on {sha[:12]}: {proc.stderr.strip()[:300]}")


def attest_mode(repo: str, policy: Policy, targets: list[str], served_url: str, dogfood_url: str,
                dry_run: bool, start_reviews: bool) -> int:
    store = store_dir(repo)
    store.mkdir(parents=True, exist_ok=True)
    lock = os.open(store / "attest.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock)
        return 0  # another attester is running; it will post what this one would
    try:
        git(repo, "fetch", "--quiet", os.environ.get("PUSH_READINESS_REMOTE", "").strip() or "origin", check=False)
        slug = github_slug(repo)
        rings = {}
        for name, url in (("production", served_url), ("dogfood", dogfood_url)):
            try:
                rings[name] = served_commit(url)
            except GateError as exc:
                print(f"review-gate: attest: {name} unreadable: {exc}", file=sys.stderr)
        if not rings:
            raise GateError("neither ring's served commit is readable; nothing to compute a verdict from")
        # An unreadable production still lets dogfood's verdict out (dogfood is its own base then).
        served = rings.get("production") or rings["dogfood"]
        dogfood = rings.get("dogfood")
        # The verdict is computed from the oldest ring that gives a clean one: production first (what a
        # production promotion asks), then dogfood (enough for a dogfood promotion, which asks from what
        # dogfood serves). A reader only trusts it for a ring whose served SHA contains that base.
        bases = [served] + ([dogfood] if dogfood and dogfood != served and is_ancestor(repo, served, dogfood) else [])
        if not targets:
            # What dogfood serves (production's candidate), the newest published image (dogfood's
            # candidate, which lags main while main's head changes no runtime path), and main itself.
            published = newest_published(repo, slug)
            targets = [t for t in (dogfood, published) if t] + [f"refs/remotes/origin/{DEFAULT_BRANCH}"]
        state_path = store / "attest.json"
        try:
            posted = json.loads(state_path.read_text())
        except (OSError, ValueError):
            posted = {}
        seen = set()
        for rev in targets:
            try:
                target = resolve(repo, rev)
            except GateError as exc:
                print(f"review-gate: attest: {exc}", file=sys.stderr)
                continue
            if target in seen or target == served:
                continue
            seen.add(target)
            usable = [b for b in bases if b != target and is_ancestor(repo, b, target)]
            if not usable:
                print(f"review-gate: attest: {target[:12]} contains neither ring's served commit "
                      f"({', '.join(b[:12] for b in bases)}); skipped")
                continue
            first = None
            for base in usable:
                verdicts = promotion_verdicts(repo, policy, base, target)
                if first is None:
                    first = (base, verdicts)
                if not verdicts:
                    break
            else:
                base, verdicts = first
            if first[1] and start_reviews and not dry_run:
                jobs = start_reviews_quietly(repo, policy, promotion_revs(repo, first[0], target), "attest")
                if jobs:
                    print(queued_note(repo, jobs))
            state, summary = attestation_summary(verdicts)
            description = f"from {base}: {summary}"
            last = posted.get(target) or {}
            fresh = _now() - float(last.get("at", 0)) < ATTEST_REPOST_S
            if (last.get("state"), last.get("description")) == (state, description) and fresh:
                continue
            print(f"review-gate: attest {target[:12]} {state}: {description}")
            if dry_run:
                continue
            try:
                post_attestation(slug, target, state, description)
            except GateError as exc:  # one failed post must not stop the others; the next run retries it
                print(f"review-gate: attest: {exc}", file=sys.stderr)
                continue
            posted[target] = {"state": state, "description": description, "at": _now()}
        if not dry_run:
            cutoff = _now() - 14 * 86400
            posted = {k: v for k, v in posted.items() if float(v.get("at", 0)) >= cutoff}
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(posted, indent=1) + "\n")
            tmp.replace(state_path)
        return 0
    finally:
        os.close(lock)


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
                      "A commit refused for an open finding needs its disposition, or a later complete review that "
                      "reads its fix (a range reaching past the reviewed commits) with nothing open about it."]
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

def paths_mode(repo: str, rng: str, paths_file: Path, as_json: bool) -> int:
    """Judge exactly the commits that touch the listed files, whatever the policy's areas say: the caller owns the
    list (an epoch's pinned verifier files are not a policy area). Exempt globs do not apply for the same reason."""
    wanted = {line.strip() for line in paths_file.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")}
    commits = [c for c in commits_in(repo, rng) if wanted.intersection(c.files)]
    verdicts = check_commits(repo, commits, load_events(repo), Landed.for_revs(repo, [rng]))
    refused = [v for v in verdicts if v.reasons]
    if as_json:
        print(json.dumps({"range": rng, "checked": len(commits), "refused": [
            {"sha": v.commit.sha, "subject": v.commit.subject, "files": sorted(wanted.intersection(v.commit.files)),
             "reasons": v.reasons} for v in refused]}))
    else:
        for v in refused:
            print(f"{v.commit.sha[:12]} {v.commit.subject[:60]}: {'; '.join(v.reasons)}")
        print(f"review-gate: {len(commits)} commit(s) in {rng} touch the listed files; {len(refused)} not cleanly reviewed.",
              file=sys.stderr)
    return 1 if refused else 0


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
    promo.add_argument("--attested", action="store_true",
                       help="decide from the GitHub review attestation instead of local receipts (a promoter off this machine)")
    blocking = sub.add_parser("blocking", help="list base..head commits that touch the blocking list (no receipts read); "
                              "exit 1 when there are any")
    blocking.add_argument("--base", required=True)
    blocking.add_argument("--head", required=True)
    attest = sub.add_parser("attest", help="post the promotion verdict for the candidates as a GitHub commit status")
    attest.add_argument("--target", action="append", default=[],
                        help="candidate (default: what dogfood serves, the newest published runtime image, origin/main)")
    attest.add_argument("--served-url", default=PRODUCTION_HEALTH_URL, help="the ring the verdict is computed from (production)")
    attest.add_argument("--dogfood-url", default=DOGFOOD_HEALTH_URL)
    attest.add_argument("--dry-run", action="store_true", help="print what it would post; post and queue nothing")
    attest.add_argument("--no-reviews", action="store_true", help="do not queue reviews of unreviewed commits")
    paths = sub.add_parser("paths", help="the verdict of every range commit that touches one of the listed files; exit 1 "
                           "when any lacks a completed review or holds an unresolved finding (the provider factory's "
                           "verifier epoch gate: it lists the files an epoch pins)")
    paths.add_argument("--range", dest="rng", required=True)
    paths.add_argument("--paths-file", required=True, help="one repo-relative path per line; '#' lines are comments")
    paths.add_argument("--json", action="store_true", help="print {range, checked, refused: [{sha, subject, files, reasons}]}")
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
        elif args.mode == "blocking":
            found = blocking_commits(repo, policy, args.base, args.head)
            for commit, areas in found:
                print(f"{commit.sha[:12]} [{', '.join(areas)}] {commit.subject[:80]}")
            if not found:
                print(f"review-gate: {args.base[:12]}..{args.head[:12]} touches nothing on the blocking list.")
            return 1 if found else 0
        elif args.mode == "attest":
            return attest_mode(repo, policy, args.target, args.served_url, args.dogfood_url, args.dry_run,
                               not args.no_reviews)
        elif args.mode == "promotion" and args.attested:
            served = resolve(repo, args.served or served_commit(args.served_url))
            target = resolve(repo, args.target)
            why = attested_refusal(repo, github_slug(repo), served, target)
            if why is None:
                print(f"review-gate: promotion OK (attested for {served[:12]}..{target[:12]}).")
                return 0
            print(f"review-gate: REFUSED promotion: {why}", file=sys.stderr)
            return 1
        elif args.mode == "promotion":
            served = args.served or served_commit(args.served_url)
            verdicts = promotion_verdicts(repo, policy, served, args.target)
            kind, what, target = "promotion", f"in {served[:12]}..{args.target[:12]} lack a completed review or hold unresolved findings", args.target
            if verdicts and args.start_reviews and not os.environ.get(OVERRIDE_ENV):
                started = start_reviews_quietly(repo, policy, promotion_revs(repo, served, args.target), "promotion")
        elif args.mode == "paths":
            return paths_mode(repo, args.rng, Path(args.paths_file), args.json)
        elif args.mode == "backfill":
            jobs = enqueue_reviews(repo, policy, [args.rng], session=None, reason="backfill")
            print(queued_note(repo, jobs) if jobs else "review-gate: nothing to start (every commit is exempt, reviewed, "
                  "queued or running, or hatch is not on PATH).")
            return 0
        else:
            landed, events = Landed.for_revs(repo, [args.rng]), load_events(repo)
            for c in commits_in(repo, args.rng):
                v = check_commits(repo, [c], events, landed)[0]
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
