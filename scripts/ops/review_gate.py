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
review; any other change to the commit does not.

  review_gate.py push [--base origin/main]
  review_gate.py promotion --target SHA (--served SHA | --served-url URL)
  review_gate.py status [--range A..B]

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
        )

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
    files: list[str]
    _patch_id: str | None = field(default=None, repr=False)

    def patch_id(self, repo: str | Path) -> str | None:
        if self._patch_id is None:
            diff = git(repo, "show", "--no-color", "--no-ext-diff", "--patch", self.sha)
            out = git(repo, "patch-id", "--stable", stdin=diff).split()
            self._patch_id = out[0] if out else ""
        return self._patch_id or None


def commits_in(repo: str | Path, *revs: str) -> list[Commit]:
    out = git(repo, "log", "--no-merges", "--reverse", "--name-only", "--format=%x01%H%x02%s", *revs)
    commits = []
    for chunk in out.split("\x01")[1:]:
        header, _, names = chunk.partition("\n")
        sha, _, subject = header.partition("\x02")
        commits.append(Commit(sha, subject.strip(), [n for n in names.splitlines() if n.strip()]))
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
        if isinstance(event, dict) and event.get("schema") == SCHEMA:
            events.append(event)
    return events


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


def check_commits(repo: str | Path, commits: list[Commit], events: list[dict]) -> list[Verdict]:
    """Coverage of each commit by review receipts: needs a complete receipt that lists its
    SHA (or patch-id), and no covering receipt may hold an unresolved gated finding."""
    reviews = [e for e in events if e.get("type") == "review"]
    by_sha: dict[str, list[dict]] = {}
    by_patch: dict[str, list[dict]] = {}
    for r in reviews:
        for c in r.get("commits", []):
            by_sha.setdefault(c.get("sha"), []).append(r)
            if c.get("patch_id"):
                by_patch.setdefault(c["patch_id"], []).append(r)
    verdicts = []
    for commit in commits:
        covering = list(by_sha.get(commit.sha, []))
        if by_patch and not any(r.get("state") == "complete" for r in covering):
            # Not reviewed under this exact SHA: a rebased copy of the same patch counts.
            seen = {r["id"] for r in covering}
            covering += [r for r in by_patch.get(commit.patch_id(repo) or "", []) if r["id"] not in seen]
        reasons = []
        complete = [r for r in covering if r.get("state") == "complete"]
        if not complete:
            if covering:
                latest = covering[-1]
                why = "; ".join(latest.get("state_reasons") or []) or "not complete"
                reasons.append(f"only a partial review ({latest['id']}: {why})")
            else:
                reasons.append("no review receipt")
        for r in covering:
            for f in open_findings(events, r):
                reasons.append(f"unresolved {f['severity']} finding {r['id']} {f['id']}: {f.get('summary', '')[:140]}")
        verdicts.append(Verdict(commit, reasons))
    return verdicts


# --- modes -----------------------------------------------------------------

def push_verdicts(repo: str | Path, policy: Policy, base: str, head: str = "HEAD") -> list[Verdict]:
    commits = commits_in(repo, f"{resolve(repo, base)}..{resolve(repo, head)}")
    gated = [(c, policy.blocking_areas(c.files)) for c in commits]
    gated = [(c, areas) for c, areas in gated if areas]
    verdicts = check_commits(repo, [c for c, _ in gated], load_events(repo))
    for v, (_, areas) in zip(verdicts, gated):
        v.areas = areas
    return [v for v in verdicts if v.reasons]


def promotion_verdicts(repo: str | Path, policy: Policy, served: str, target: str) -> list[Verdict]:
    served, target = resolve(repo, served), resolve(repo, target)
    revs = [f"{served}..{target}"]
    boundary = grandfather_boundary(repo)
    if boundary:
        revs.append(f"^{boundary}")
    commits = [c for c in commits_in(repo, *revs) if not policy.exempt_commit(c.subject, c.files)]
    verdicts = check_commits(repo, commits, load_events(repo))
    for v in verdicts:
        v.areas = policy.blocking_areas(v.commit.files)
    return [v for v in verdicts if v.reasons]


def served_commit(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - operator-supplied health URL
            body = json.load(resp)
    except (OSError, ValueError) as exc:
        raise GateError(f"could not read the served commit from {url}: {exc}")
    sha = ((body.get("build") or {}).get("commit") or "").strip() if isinstance(body, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GateError(f"{url} does not report a full build.commit (got {sha!r})")
    return sha


# --- reporting -------------------------------------------------------------

def refusal(repo: str, kind: str, what: str, verdicts: list[Verdict]) -> str:
    lines = [f"review-gate: REFUSED {kind}: {len(verdicts)} commit(s) {what}", ""]
    for v in verdicts:
        area = f" [{', '.join(v.areas)}]" if v.areas else ""
        lines.append(f"  {v.commit.sha[:12]} {v.commit.subject[:80]}{area}")
        lines.extend(f"      - {reason}" for reason in v.reasons)
    unreviewed = [v.commit.sha for v in verdicts if any(r.startswith(("no review", "only a partial")) for r in v.reasons)]
    lines.append("")
    if unreviewed:
        first, last = unreviewed[0], unreviewed[-1]
        lines += [
            "Get a receipt (reviews an exact commit range in a throwaway checkout; --no-intent for someone else's commits):",
            f"  hatch review -C {repo} --base {first[:12]}^ --head {last[:12]} --no-intent",
            "A rebase after review keeps the receipt. A partial review (a pass hit its budget) does not count.",
        ]
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
    promo = sub.add_parser("promotion", help="promotion rule: every code commit since the served SHA is reviewed")
    promo.add_argument("--target", required=True)
    group = promo.add_mutually_exclusive_group(required=True)
    group.add_argument("--served")
    group.add_argument("--served-url")
    status = sub.add_parser("status", help="print the verdict of every commit of a range (never refuses)")
    status.add_argument("--range", dest="rng", default="origin/main..HEAD")
    args = parser.parse_args(argv)

    repo = str(Path(args.repo).resolve())
    policy_path = args.policy or str(Path(__file__).resolve().parent / "review-policy.toml")
    try:
        policy = Policy.load(policy_path, args.name or repo_name(repo))
        if args.mode == "push":
            if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                print("review-gate: push check skipped in CI (receipts live on the author's machine).")
                return 0
            if not git(repo, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}", check=False).strip():
                print(f"review-gate: {args.base} not found; push check skipped.")
                return 0
            verdicts = push_verdicts(repo, policy, args.base, args.head)
            kind, what, target = "push", "touch the blocking list without a completed review", args.head
        elif args.mode == "promotion":
            served = args.served or served_commit(args.served_url)
            verdicts = promotion_verdicts(repo, policy, served, args.target)
            kind, what, target = "promotion", f"in {served[:12]}..{args.target[:12]} lack a completed review or hold unresolved findings", args.target
        else:
            base, _, head = args.rng.partition("..")
            for c in commits_in(repo, args.rng):
                v = check_commits(repo, [c], load_events(repo))[0]
                areas = policy.blocking_areas(c.files)
                exempt = policy.exempt_commit(c.subject, c.files)
                print(f"{c.sha[:12]} {'exempt' if exempt else 'reviewed' if not v.reasons else 'NOT OK '} "
                      f"{','.join(areas) or '-'} {c.subject[:60]} {'; '.join(v.reasons)}")
            return 0
    except GateError as exc:
        print(f"review-gate: {exc}", file=sys.stderr)
        return 2
    if not verdicts:
        print(f"review-gate: {kind} OK.")
        return 0
    if override(repo, kind, target, verdicts):
        return 0
    print(refusal(repo, kind, what, verdicts), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
