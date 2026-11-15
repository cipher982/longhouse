#!/usr/bin/env python3
"""Ring promoter: move dogfood and production forward on their own, never backwards.

Run by the Promote Rings workflow (.github/workflows/promote-rings.yml) after every image
publication, archive, canary, Hosted Live QA and CI completion, and Sauron's
`longhouse-ring-wake` tick while a ring trails. Each run is stateless: it reads what each ring serves
and what main holds, decides, and calls the promote scripts, which keep every gate they had.

  dogfood     target = the newest main commit whose runtime image is published.
              - target contains what dogfood serves and served..target touches no sensitive path:
                `promote-dogfood.sh --fast-lane` (published image is enough).
              - otherwise `promote-dogfood.sh target`: a canary receipt for target is required.
                Refused means waiting for qualification.
  production  `promote-production.sh` for what dogfood serves, when production serves an ancestor
              of it. The contract is unchanged (dogfood digest, Hosted Live QA, previous-release engine,
              soak, archive); a refusal records which gates it is waiting on.

Every decision is printed as JSON, appended to the job summary, and posted as a GitHub commit
status (`longhouse/dogfood` on the dogfood target, `longhouse/production` on what dogfood serves),
which is what the Sauron `longhouse-ring-follow` job reads to alert once production has trailed
dogfood for too long. Exit 1 only when a promotion started and failed (a red run is worth a look);
waiting is exit 0.

  ring_promoter.py [--dry-run] [--json PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = os.environ.get("GITHUB_REPOSITORY", "cipher982/longhouse")
DOGFOOD = os.environ.get("SUBDOMAIN") or os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN") or "david010"
DOGFOOD_HEALTH = os.environ.get("DOGFOOD_HEALTH_URL") or f"https://{DOGFOOD}.longhouse.ai/api/health"
PRODUCTION_HEALTH = os.environ.get("DEMO_HEALTH_URL") or "https://longhouse.ai/api/health"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FAST_LANE_NOT_ALLOWED = 3  # promote-dogfood.sh --fast-lane: the range has sensitive paths
TERMINAL_TRUTH_WAITING = 4  # promote-dogfood.sh: a live-preview change has no terminal-truth status yet, or it is pending
TERMINAL_TRUTH_REFUSED = 5  # promote-dogfood.sh: the terminal-truth status failed, errored or was not posted by Actions


def log(message: str) -> None:
    print(f"ring-promoter: {message}", file=sys.stderr, flush=True)


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def is_ancestor(older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", older, newer],
                          capture_output=True).returncode == 0


def served(url: str) -> str | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "longhouse-ring-promoter/1"})
        with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310 - fixed ring health URLs
            body = json.load(resp)
    except (OSError, ValueError) as exc:
        log(f"cannot read {url}: {exc}")
        return None
    sha = str(((body or {}).get("build") or {}).get("commit") or "")
    return sha if SHA_RE.match(sha) else None


def gone(url: str) -> bool:
    """True when the ring answers 404 or 410: no production surface is served there, which is not an
    unreadable ring. Sauron's ring-follow job reads the same two codes the same way."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "longhouse-ring-promoter/1"})
        urllib.request.urlopen(request, timeout=15).close()  # noqa: S310 - fixed ring health URLs
    except urllib.error.HTTPError as exc:
        return exc.code in (404, 410)
    except OSError:
        return False
    return False


def gh_json(*args: str):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "null")


def published_candidates(main: str, limit: int = 30) -> list[str]:
    """Main commits with a successful runtime image publication, newest first."""
    runs = gh_json("api", f"repos/{REPO}/actions/workflows/runtime-image.yml/runs?branch=main&status=success&per_page={limit}")
    found: list[str] = []
    for run in sorted(runs.get("workflow_runs") or [], key=lambda r: int(r.get("run_number") or 0), reverse=True):
        sha = str(run.get("head_sha") or "")
        if (run.get("event") in ("push", "workflow_dispatch") and SHA_RE.match(sha) and sha not in found
                and is_ancestor(sha, main)):
            found.append(sha)
    return found


def required_terminal_truth_commit(base: str, head: str) -> str | None:
    """The commit whose Terminal Truth verdict a promotion of base..head needs (None: no live-preview change)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dogfood_sensitive_paths

    return dogfood_sensitive_paths.newest_live_preview_commit(ROOT, base, head)


def run_script(argv: list[str], env: dict | None = None) -> tuple[int, str, str, float]:
    started = time.monotonic()
    log(f"running {' '.join(argv)}")
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, env={**os.environ, **(env or {})})
    sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr, round(time.monotonic() - started, 1)


def tail(text: str, lines: int = 6) -> str:
    return " | ".join(line.strip() for line in text.strip().splitlines()[-lines:] if line.strip())[:600]


MAX_CANDIDATES = 6  # published images tried per run, newest first; each try is one gate check, not a deploy


def decide_dogfood(main: str, dry_run: bool) -> dict:
    """Promote the newest published commit whose gates are all green, not only the newest published one.

    Terminal Truth and Deploy and Verify run one at a time and GitHub keeps one pending run, so with pushes every few
    minutes the newest commit rarely has its own verdict while an older one does. Insisting on the newest left dogfood
    on one commit for hours on 2026-11-15 (7861a9eb6 green, 81099b157 waiting). A candidate that is waiting on a
    gate, or whose Terminal Truth verdict is red, is passed over for the next older one that contains what dogfood
    serves; the newest candidate's own outcome is what is reported when none can go. A commit whose own or covering
    Terminal Truth failed is never promoted: promote-dogfood.sh refuses it (exit 5) and the promoter only moves on.
    """
    current = served(DOGFOOD_HEALTH)
    candidates = published_candidates(main)
    newest = candidates[0] if candidates else None
    d = {"ring": "dogfood", "served": current, "target": newest}
    if current is None:
        return {**d, "action": "waiting", "reason": f"{DOGFOOD} health is unreadable"}
    if newest is None:
        return {**d, "action": "up_to_date", "reason": "no published runtime image on main"}
    if newest == current or is_ancestor(newest, current):
        return {**d, "action": "up_to_date", "reason": f"{DOGFOOD} serves {current[:12]}, newest published is {newest[:12]}"}
    if not is_ancestor(current, newest):
        return {**d, "action": "refused", "reason": f"{newest[:12]} does not contain what {DOGFOOD} serves; never sideways"}
    if dry_run:
        return {**d, "action": "would_promote"}
    ahead = [c for c in candidates if c != current and not is_ancestor(c, current) and is_ancestor(current, c)]
    first: dict | None = None
    skipped: list[str] = []
    passed_over: set[str] = set()  # Terminal Truth commits already seen waiting or red: older candidates share the verdict
    for candidate in ahead[:MAX_CANDIDATES]:
        needed = required_terminal_truth_commit(current, candidate)
        if needed and needed in passed_over:
            continue
        result = attempt_dogfood({**d, "target": candidate}, candidate)
        first = first or result
        if result["action"] == "promoted":
            if skipped:
                result["reason"] = f"newer published {', '.join(s[:12] for s in skipped)} not ready: {first['reason']}"[:600]
            return result
        movable = (result["action"] == "waiting" and "lock" not in (result.get("waiting_on") or [])) or (
            result["action"] == "refused" and result.get("waiting_on_verdict") is True)
        if not movable:
            return first if candidate == ahead[0] else result
        skipped.append(candidate)
        if needed:
            passed_over.add(needed)
    return {**(first or d), "target": newest}


def terminal_truth_outcome(d: dict, rc: int, err: str, lane: str) -> dict:
    """A live-preview change needs its passing `longhouse/terminal-truth` status (promote-dogfood.sh): exit 4 is a
    missing or pending status with no covering later run (wait for Terminal Truth), exit 5 a failed one (a refusal)."""
    if rc == TERMINAL_TRUTH_WAITING:
        return {**d, "action": "waiting", "lane": lane, "waiting_on": ["terminal-truth"], "reason": tail(err, 2)}
    return {**d, "action": "refused", "lane": lane, "waiting_on_verdict": True, "reason": tail(err, 2)}


def attempt_dogfood(d: dict, target: str) -> dict:
    """One promote-dogfood.sh attempt for one target: fast lane first, then the canary-qualified path."""
    rc, out, err, elapsed = run_script(["bash", "scripts/ops/promote-dogfood.sh", "--fast-lane", target])
    if rc == 0:
        return {**d, "action": "promoted", "lane": "fast-lane", "elapsed_s": elapsed, "reason": tail(out, 2)}
    if rc in (TERMINAL_TRUTH_WAITING, TERMINAL_TRUTH_REFUSED):
        return terminal_truth_outcome(d, rc, err, "fast-lane")
    if rc != FAST_LANE_NOT_ALLOWED:
        return {**d, "action": "failed", "lane": "fast-lane", "elapsed_s": elapsed, "reason": tail(err)}
    rc, out, err, elapsed = run_script(["bash", "scripts/ops/promote-dogfood.sh", target])
    if rc == 0:
        return {**d, "action": "promoted", "lane": "canary-qualified", "elapsed_s": elapsed, "reason": tail(out, 2)}
    if rc in (TERMINAL_TRUTH_WAITING, TERMINAL_TRUTH_REFUSED):
        return terminal_truth_outcome(d, rc, err, "canary-qualified")
    # A missing canary verification receipt means Deploy and Verify is still running
    # or a newer push superseded this target.
    waiting = []
    if "canary verification receipt" in err:
        waiting.append("canary")
    if "could not take" in err and "lock" in err:
        waiting.append("lock")
    if waiting:
        return {**d, "action": "waiting", "lane": "canary-qualified", "waiting_on": waiting,
                "reason": "sensitive-path range: " + tail(err, 3)}
    return {**d, "action": "failed", "lane": "canary-qualified", "elapsed_s": elapsed, "reason": tail(err)}


def decide_production(dry_run: bool) -> dict:
    dogfood = served(DOGFOOD_HEALTH)
    current = served(PRODUCTION_HEALTH)
    d = {"ring": "production", "served": current, "target": dogfood}
    if dogfood is not None and current is None and gone(PRODUCTION_HEALTH):
        return {**d, "action": "paused", "reason": "production is paused: its health answers 404 or 410"}
    if dogfood is None or current is None:
        return {**d, "action": "waiting", "reason": "a ring's health is unreadable"}
    if current == dogfood:
        return {**d, "action": "up_to_date", "reason": f"production serves what {DOGFOOD} serves ({dogfood[:12]})"}
    if not is_ancestor(current, dogfood):
        return {**d, "action": "refused", "reason": f"production {current[:12]} is not behind {DOGFOOD} {dogfood[:12]}; never backwards"}
    with tempfile.TemporaryDirectory(prefix="ring-promoter-") as receipts:
        argv = ["bash", "scripts/ops/promote-production.sh"] + (["--check"] if dry_run else []) + [dogfood]
        rc, out, err, elapsed = run_script(argv, {"PROMOTION_RECEIPT_DIR": receipts})
        saved = sorted(Path(receipts).glob("production-*.json"))
        receipt = {}
        if saved:
            try:
                receipt = json.loads(saved[-1].read_text())
            except ValueError:
                receipt = {}
    gates = receipt.get("gates") or {}
    refused = {name: g.get("refusal") for name, g in gates.items() if isinstance(g, dict) and not g.get("ok")}
    if rc == 0:
        return {**d, "action": "would_promote" if dry_run else "promoted", "elapsed_s": elapsed,
                "deployment": (receipt.get("promotion") or {}).get("deployment_id")}
    if refused:
        return {**d, "action": "waiting", "waiting_on": sorted(refused), "refusals": refused,
                "reason": "; ".join(f"{k}: {v}" for k, v in sorted(refused.items()))[:600]}
    if "could not take the production lock" in err:
        return {**d, "action": "waiting", "waiting_on": ["lock"], "reason": tail(err, 2)}
    return {**d, "action": "failed", "elapsed_s": elapsed, "reason": tail(err),
            "stopped": (receipt.get("promotion") or {}).get("outcome")}


STATE = {"promoted": "success", "up_to_date": "success", "would_promote": "pending",
         "waiting": "pending", "paused": "pending", "refused": "failure", "failed": "failure"}


def describe(d: dict) -> str:
    action = d["action"]
    lane = f" ({d['lane']})" if d.get("lane") else ""
    if d.get("waiting_on"):
        text = f"waiting{lane} on " + ", ".join(d["waiting_on"])
    else:
        text = action.replace("_", " ") + lane
        if d.get("target") and action in ("promoted", "would_promote"):
            text += f" {d['target'][:12]}"
        elif d.get("reason") and action in ("failed", "refused", "paused"):
            text += ": " + d["reason"]
    return text[:140]


def post_status(d: dict, run_url: str) -> None:
    # dogfood: on the commit it tried to move to (nothing to say when it is already there);
    # production: on what dogfood serves, the commit production is waiting to take.
    sha = d.get("target")
    if not sha or (d["ring"] == "dogfood" and d["action"] == "up_to_date"):
        return
    args = ["api", "--method", "POST", f"repos/{REPO}/statuses/{sha}", "-f", f"state={STATE[d['action']]}",
            "-f", f"context=longhouse/{d['ring']}", "-f", f"description={describe(d)}"]
    if run_url:
        args += ["-f", f"target_url={run_url}"]
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"could not post the {d['ring']} status on {sha[:12]}: {proc.stderr.strip()[:200]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="decide and print; promote and post nothing")
    parser.add_argument("--json", help="also write the decision record here")
    args = parser.parse_args(argv)
    started = time.time()
    git("fetch", "--quiet", "origin", "main", check=False)
    main_sha = git("rev-parse", "origin/main")
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), "main": main_sha, "run": run_url,
              "trigger": os.environ.get("PROMOTER_TRIGGER", "manual")}
    try:
        record["dogfood"] = decide_dogfood(main_sha, args.dry_run)
    except Exception as exc:  # noqa: BLE001 - one ring's fault must not stop the other's decision
        record["dogfood"] = {"ring": "dogfood", "action": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    try:
        record["production"] = decide_production(args.dry_run)
    except Exception as exc:  # noqa: BLE001
        record["production"] = {"ring": "production", "action": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    record["elapsed_s"] = round(time.time() - started, 1)
    text = json.dumps(record, indent=1)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## Promote Rings\n\n")
            for ring in ("dogfood", "production"):
                d = record[ring]
                fh.write(f"- **{ring}**: {describe(d)} (serves `{(d.get('served') or '?')[:12]}`, target "
                         f"`{(d.get('target') or '?')[:12]}`)\n")
                if d.get("reason"):
                    fh.write(f"  - {d['reason']}\n")
            fh.write(f"\n```json\n{text}\n```\n")
    if not args.dry_run:
        for ring in ("dogfood", "production"):
            post_status(record[ring], run_url)
    return 1 if any(record[r]["action"] == "failed" for r in ("dogfood", "production")) else 0


if __name__ == "__main__":
    sys.exit(main())
