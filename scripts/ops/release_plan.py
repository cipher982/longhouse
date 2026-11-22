#!/usr/bin/env python3
"""Decisions for the Release workflow (.github/workflows/release.yml).

  release_plan.py plan --sha SHA [--dogfood] [--rollback]
  release_plan.py terminal-truth --commit COMMIT [--wait SECONDS]

`plan` decides, for one exact commit, whether a release has anything to do and which gates apply. It reads
GitHub (is CI green for SHA) and what dogfood serves right now, writes `key=value` lines to $GITHUB_OUTPUT
(or stdout) and prints the same as JSON:

  deploy      true when the runtime image must be built and deployed; false when there is nothing to do
  reason      one line saying why
  sensitive   true when served..SHA touches a path in dogfood-sensitive-paths.toml: Hosted Live QA gates dogfood
  tt_commit   the newest served..SHA commit that touches a live-preview path; its Terminal Truth verdict gates
              dogfood (empty: none required)
  served      the commit dogfood serves

Without --dogfood the release is canary-only: deploy is true, no gate applies.

Exit 0 on a decision (including "nothing to do"), 1 when it could not decide (dogfood unreadable, a lookup failed):
a red run is worth a look, a skipped deploy is not.

`terminal-truth` is the dogfood gate. Exit 0: COMMIT has a passing `longhouse/terminal-truth` status posted by
GitHub Actions, or a later main commit that contains it has one (terminal_truth_cover.py). Exit 4: still missing
or pending after --wait. Exit 5: failed, errored, or not posted by Actions; a failed verdict is never overridden.
Terminal Truth runs beside CI and takes about as long, so the release waits here instead of being re-triggered.

Dogfood holds irreplaceable history, so these gates are the contract the deploy keeps:
  - never move dogfood sideways or backwards (rollback is explicit: --rollback, which always takes the QA gate and
    also allows redeploying the served commit);
  - a range touching sensitive paths is qualified on Hosted Live QA first;
  - a live-preview change needs its Terminal Truth verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dogfood_sensitive_paths as paths  # noqa: E402
import terminal_truth_cover as cover  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REPO = os.environ.get("GITHUB_REPOSITORY", "cipher982/longhouse")
DOGFOOD = os.environ.get("SUBDOMAIN") or os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN") or "david010"
HEALTH_URL = os.environ.get("DOGFOOD_HEALTH_URL") or f"https://{DOGFOOD}.longhouse.ai/api/health"
CI_WORKFLOW = "contract-first-ci.yml"
FILTERS = ROOT / ".github" / "path-filters.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CONTEXT = cover.CONTEXT
WAITING, REFUSED = 4, 5


def log(message: str) -> None:
    print(f"release-plan: {message}", file=sys.stderr, flush=True)


def gh_json(*args: str):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "null")


def is_ancestor(older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", older, newer], capture_output=True).returncode == 0


def served_commit(url: str = HEALTH_URL) -> str | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "longhouse-release/1"})
        with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310 - fixed dogfood health URL
            body = json.load(resp)
    except (OSError, ValueError) as exc:
        log(f"cannot read {url}: {exc}")
        return None
    sha = str(((body or {}).get("build") or {}).get("commit") or "")
    return sha if SHA_RE.match(sha) else None


def runtime_image_globs(filters: Path = FILTERS) -> list[str]:
    """The `runtime_image` list of .github/path-filters.yml: the inputs of the runtime image."""
    globs: list[str] = []
    inside = False
    for line in filters.read_text(encoding="utf-8").splitlines():
        if re.match(r"^runtime_image:\s*$", line):
            inside = True
        elif inside and re.match(r"^\S", line):
            break
        elif inside:
            match = re.match(r"^\s+-\s+['\"]?([^'\"#]+?)['\"]?\s*(?:#.*)?$", line)
            if match:
                globs.append(match.group(1))
    if not globs:
        raise ValueError(f"{filters} has no runtime_image filter")
    return globs


def runtime_changed(base: str, head: str) -> bool:
    patterns = [paths.compile_glob(g) for g in runtime_image_globs()]
    return any(p.fullmatch(name) for name in paths.changed_paths(ROOT, base, head) for p in patterns)


def ci_green(sha: str) -> bool:
    runs = gh_json("api", f"repos/{REPO}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={sha}&status=success&per_page=5")
    return bool((runs or {}).get("workflow_runs"))


def plan(sha: str, dogfood: bool, rollback: bool) -> dict:
    sha = paths.resolve(ROOT, sha)
    if not ci_green(sha):
        return {"sha": sha, "deploy": "false", "reason": f"no green CI run for {sha[:12]}: releases take only green commits"}
    result = {"sha": sha, "deploy": "true", "sensitive": "false", "tt_commit": "", "served": ""}
    if not dogfood:
        return {**result, "reason": "canary-only release"}
    served = served_commit()
    if served is None:
        raise RuntimeError(f"cannot read what {DOGFOOD} serves from {HEALTH_URL}; refusing to decide blind")
    result["served"] = served
    if not paths.git(ROOT, "cat-file", "-t", served, check=False).strip():
        subprocess.run(["git", "-C", str(ROOT), "fetch", "--quiet", "origin"], capture_output=True)
    if rollback:  # the explicit operator override: any commit, the served one included (a redeploy); always QA gated
        return {**result, "sensitive": "true", "reason": f"rollback of {DOGFOOD} from {served[:12]} to {sha[:12]} (QA gated)"}
    if served == sha:
        return {**result, "deploy": "false", "reason": f"{DOGFOOD} already serves {sha[:12]}"}
    if not is_ancestor(served, sha):
        return {**result, "deploy": "false",
                "reason": f"{sha[:12]} does not contain {served[:12]}, which {DOGFOOD} serves; never moves dogfood backwards or sideways"}
    if not runtime_changed(served, sha):
        return {**result, "deploy": "false", "reason": f"no runtime input changed in {served[:12]}..{sha[:12]}"}
    sensitive = paths.has_sensitive_paths(ROOT, served, sha)
    tt = paths.newest_live_preview_commit(ROOT, served, sha) or ""
    return {**result, "sensitive": str(sensitive).lower(), "tt_commit": tt,
            "reason": f"{served[:12]}..{sha[:12]} changes the runtime" + (" (sensitive paths: QA gated)" if sensitive else "")}


def terminal_truth_state(commit: str) -> tuple[int, str]:
    """(exit code, message) for one look at the verdict of `commit`."""
    statuses = gh_json("api", f"repos/{REPO}/commits/{commit}/statuses?per_page=100")
    rows = [s for s in statuses or [] if s.get("context") == CONTEXT]
    row = rows[0] if rows else None  # newest first
    state = (row or {}).get("state", "")
    url = (row or {}).get("target_url") or ""
    if state == "success":
        creator = ((row or {}).get("creator") or {}).get("login") or ""
        if creator != "github-actions[bot]":
            return REFUSED, f"the terminal-truth status on {commit[:12]} was posted by '{creator}', not GitHub Actions"
        return 0, f"terminal-truth: {commit[:12]} passed ({url})"
    if state in ("failure", "error"):
        return REFUSED, f"the terminal-truth status on {commit[:12]} is {state} ({url}): live text regressed on a live-preview change"
    # No status or pending: a later main commit that contains it and passed covers it.
    try:
        runs = gh_json("api", f"repos/{REPO}/actions/workflows/contract-first-ci.yml/runs?branch=main&status=success&per_page=30")
        found = cover.covering(str(ROOT), "origin/main", commit, (runs or {}).get("workflow_runs") or [],
                               lambda sha: gh_json("api", f"repos/{REPO}/commits/{sha}/statuses?per_page=30"))
    except (RuntimeError, ValueError) as exc:
        log(f"cover lookup failed: {exc}")
        found = None
    if found:
        return 0, f"terminal-truth: {commit[:12]} has no verdict of its own; {found[0][:12]}, a later main commit that contains it, passed ({found[1]})"
    return WAITING, f"the terminal-truth status on {commit[:12]} is {state or 'missing'}"


def wait_terminal_truth(commit: str, wait: int) -> int:
    deadline = time.monotonic() + wait
    while True:
        code, message = terminal_truth_state(commit)
        print(message, flush=True)
        if code != WAITING or time.monotonic() >= deadline:
            return code
        time.sleep(60)


def emit(result: dict) -> None:
    lines = [f"{key}={value}" for key, value in result.items()]
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print(json.dumps(result, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--sha", required=True)
    p.add_argument("--dogfood", action="store_true")
    p.add_argument("--rollback", action="store_true")
    t = sub.add_parser("terminal-truth")
    t.add_argument("--commit", required=True)
    t.add_argument("--wait", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            emit(plan(args.sha, args.dogfood, args.rollback))
            return 0
        return wait_terminal_truth(paths.resolve(ROOT, args.commit), args.wait)
    except (RuntimeError, ValueError) as exc:
        log(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
