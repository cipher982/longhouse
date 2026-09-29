#!/usr/bin/env python3
"""Decide a Hosted Live QA verdict: passed, superseded, or failed.

Every push to main redeploys the one shared hosted canary, and a QA run takes
about six minutes, so a run for commit X often finishes after a newer commit Y
has replaced X on the canary. That run did not find a defect in X. It measured
a moving target: its steps hit the ~40 s drain and restart (HTTP 502/503 on the
shipper bench and the cohort journey), and the final source check saw Y. From
09-26 to 09-29 every one of the 19 failed runs (of 100) overlapped a newer
canary deployment, and no green run overlapped one that targeted the canary.

A run that did not verify X is not a failure of X. Y contains X and its own
Deploy and Verify dispatches its own QA, so a QA run whose canary was replaced
mid-run stands down as `superseded`, the same way main CI drops a queued run
for a commit that is no longer main's head. The evidence is the canary's own
deployment receipts (control plane `GET /api/deployments`), not timing luck: a
newer deployment that targeted this canary and overlapped the run, or a canary
that now serves a different commit than the one QA verified before it started.

    passed      every QA step succeeded and the canary still serves the
                verified commit, clean.
    superseded  the canary was replaced during the run (whatever QA steps
                reported after that are not a verdict on the verified commit).
    failed      a QA step failed, or the canary is dirty or unreadable, and no
                deployment replaced the canary during the run. Missing evidence
                is never read as a supersession.

Exit 0 for passed and superseded, 1 for failed. Stdlib only.

`--receipt PATH` writes the verdict as JSON (`longhouse.hosted-qa-verdict.v1`),
which the workflow uploads as the artifact `hosted-live-qa-verdict-<sha>`. It is
the only Hosted Live QA evidence `make promote-production` accepts: a green run
is not enough (a superseded run also exits 0 and concludes success), so the
promotion reads the verdict itself and treats anything but `passed` as no
evidence (scripts/ops/promotion_gates.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Callable

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# A deployment in these states never touched the canary's serving container.
NOT_MUTATING = {"dry_run", "superseded"}
IN_FLIGHT = {"queued", "active"}
# A real cutover takes about a minute; an "active" row older than this is a
# stale row, and must not turn every later verdict into a supersession.
IN_FLIGHT_MAX_AGE = timedelta(minutes=15)
REQUIRED_STEP = "qa_live"
# Checks that never touch the canary: a failure is the commit's, whatever the
# canary was doing, so no supersession can excuse it.
CANARY_INDEPENDENT = ("cohort_contracts",)


class EvidenceUnavailable(Exception):
    """The control plane could not be read; absence of evidence, not evidence."""


@dataclass
class Verdict:
    verdict: str
    reason: str
    failed_steps: list[str] = field(default_factory=list)
    canary_now: str | None = None
    superseded_by: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict in {"passed", "superseded"}


def parse_ts(value: str) -> datetime:
    # The control plane stores UTC without an offset (2026-09-29T18:06:25.123456).
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def canary_commit(health: dict[str, Any] | None) -> str | None:
    if not isinstance(health, dict):
        return None
    build = health.get("build") if isinstance(health.get("build"), dict) else {}
    for candidate in (health.get("source_sha"), build.get("source_sha"), build.get("commit")):
        if isinstance(candidate, str) and SHA_RE.match(candidate):
            return candidate
    return None


def failed_steps(outcomes: dict[str, str]) -> list[str]:
    failed = sorted(name for name, outcome in outcomes.items() if outcome in {"failure", "cancelled"})
    # qa_live gates every other step, so "skipped" or absent means QA never ran.
    if outcomes.get(REQUIRED_STEP) != "success" and REQUIRED_STEP not in failed:
        failed.append(f"{REQUIRED_STEP} (did not run)")
    return failed


def candidate_deployments(
    rows: list[dict[str, Any]],
    *,
    started_at: datetime,
    now: datetime,
    own_deployment: str,
) -> list[dict[str, Any]]:
    """Deployments whose window intersects the run, judged from the list alone."""
    found = []
    for row in rows:
        status = str(row.get("status") or "")
        if row.get("id") == own_deployment or status in NOT_MUTATING or not row.get("created_at"):
            continue
        created = parse_ts(row["created_at"])
        completed = parse_ts(row["completed_at"]) if row.get("completed_at") else None
        if created > now:
            continue
        if completed is not None:
            overlaps = completed >= started_at
        elif status in IN_FLIGHT:
            overlaps = created >= started_at - IN_FLIGHT_MAX_AGE
        else:
            # paused/failed without a completion time: only its start is known.
            overlaps = created >= started_at
        if overlaps:
            found.append(row)
    return found


def targets_canary(detail: dict[str, Any], subdomain: str) -> bool:
    """A deployment with no target for this canary (a pointer-only promotion, or
    another tenant's lifecycle test) did not touch it."""
    return any(isinstance(t, dict) and t.get("subdomain") == subdomain for t in detail.get("targets") or [])


def superseding_deployments(
    *,
    started_at: datetime,
    now: datetime,
    subdomain: str,
    own_deployment: str,
    list_deployments: Callable[[], list[dict[str, Any]]],
    get_deployment: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = candidate_deployments(list_deployments(), started_at=started_at, now=now, own_deployment=own_deployment)
    return [row for row in rows if targets_canary(get_deployment(str(row["id"])), subdomain)]


def decide(
    *,
    expected_sha: str,
    started_at: datetime,
    now: datetime,
    subdomain: str,
    own_deployment: str,
    step_outcomes: dict[str, str],
    health: dict[str, Any] | None,
    list_deployments: Callable[[], list[dict[str, Any]]],
    get_deployment: Callable[[str], dict[str, Any]],
) -> Verdict:
    failed = failed_steps(step_outcomes)
    served = canary_commit(health)
    moved = served is not None and served != expected_sha

    own_failures = [name for name in CANARY_INDEPENDENT if step_outcomes.get(name) in {"failure", "cancelled"}]
    if own_failures:
        return Verdict("failed", f"a check that does not depend on the canary failed: {', '.join(own_failures)}", failed, served)

    if served == expected_sha:
        build = health.get("build") if isinstance(health, dict) and isinstance(health.get("build"), dict) else {}
        if build.get("dirty") is not False:
            return Verdict("failed", f"canary serves {served} but reports a dirty or unlabeled build", failed, served)
        if not failed:
            return Verdict("passed", f"every QA step succeeded and the canary still serves {expected_sha}", [], served)

    # A QA step failed, the canary changed under the run, or it cannot be read.
    try:
        replaced_by = superseding_deployments(
            started_at=started_at,
            now=now,
            subdomain=subdomain,
            own_deployment=own_deployment,
            list_deployments=list_deployments,
            get_deployment=get_deployment,
        )
        evidence_error = None
    except (EvidenceUnavailable, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        replaced_by, evidence_error = [], f"{type(exc).__name__}: {exc}"

    if moved or replaced_by:
        if replaced_by:
            ids = ", ".join(f"{row['id']} ({(row.get('source_sha') or '?')[:9]}, {row.get('status')})" for row in replaced_by)
            reason = f"the canary was redeployed during the run by {ids}"
        else:
            reason = f"the canary now serves {served}, not the verified {expected_sha}"
        return Verdict("superseded", reason, failed, served, replaced_by)

    if failed:
        reason = f"QA step(s) failed with no canary deployment overlapping the run: {', '.join(failed)}"
    else:
        reason = "the canary health endpoint could not be read after QA and no deployment explains it"
    if evidence_error:
        reason += f" (deployment receipts unavailable: {evidence_error})"
    return Verdict("failed", reason, failed, served)


RECEIPT_SCHEMA = "longhouse.hosted-qa-verdict.v1"
IMAGE_DIGEST_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def tested_image(own_deployment: str, get_deployment: Callable[[str], dict[str, Any]]) -> str | None:
    """The digest the canary was deployed with, when its receipt can be read.

    Best effort: the verdict never depends on it, and a promotion only refuses a
    receipt whose digest it can read and that differs from the one promoted.
    """
    if not own_deployment:
        return None
    try:
        image = get_deployment(own_deployment).get("image_digest")
    except (EvidenceUnavailable, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return image if isinstance(image, str) and IMAGE_DIGEST_RE.match(image) else None


def build_receipt(
    result: Verdict,
    *,
    expected_sha: str,
    started_at: datetime,
    decided_at: datetime,
    subdomain: str,
    own_deployment: str,
    step_outcomes: dict[str, str],
    image_digest: str | None,
    environ: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "verdict": result.verdict,
        "reason": result.reason,
        "verified_sha": expected_sha,
        "canary_sha": result.canary_now,
        "canary_subdomain": subdomain,
        "canary_deployment_id": own_deployment or None,
        "canary_image_digest": image_digest,
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decided_at": decided_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "failed_steps": result.failed_steps,
        "steps": step_outcomes,
        "superseded_by": [row.get("id") for row in result.superseded_by],
        "run": {
            "repository": environ.get("GITHUB_REPOSITORY"),
            "workflow": environ.get("GITHUB_WORKFLOW"),
            "id": environ.get("GITHUB_RUN_ID"),
            "attempt": environ.get("GITHUB_RUN_ATTEMPT"),
        },
    }


def http_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15) -> Any:
    # Cloudflare answers the default Python-urllib agent with 403 (error 1010).
    request = urllib.request.Request(url, headers={"User-Agent": "longhouse-hosted-qa-verdict/1", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
        return json.load(response)


def read_health(url: str, *, attempts: int = 3, delay: float = 5) -> dict[str, Any] | None:
    for attempt in range(attempts):
        try:
            payload = http_json(url)
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(delay)
    return None


def control_plane_readers(base: str, token: str) -> tuple[Callable[[], list[dict[str, Any]]], Callable[[str], dict[str, Any]]]:
    headers = {"X-Admin-Token": token}
    base = base.rstrip("/")

    def list_deployments() -> list[dict[str, Any]]:
        return http_json(f"{base}/api/deployments?limit=50", headers=headers).get("deployments") or []

    def get_deployment(deployment_id: str) -> dict[str, Any]:
        return http_json(f"{base}/api/deployments/{urllib.parse.quote(deployment_id, safe='')}", headers=headers)

    return list_deployments, get_deployment


def emit(result: Verdict, args: argparse.Namespace) -> None:
    lines = [f"Hosted Live QA verdict: {result.verdict}: {result.reason}"]
    if result.failed_steps and result.verdict != "failed":
        lines.append(f"QA steps that reported failure and are not a verdict on {args.expected_sha[:9]}: {', '.join(result.failed_steps)}")
    print("\n".join(lines))
    level = {"passed": "notice", "superseded": "notice", "failed": "error"}[result.verdict]
    if result.verdict == "superseded" and any(row.get("status") not in {"success", "queued", "active"} for row in result.superseded_by):
        # No successor QA follows a deployment that did not succeed, so nothing
        # else will judge the commit this run stood down for.
        level = "warning"
    print(f"::{level} title=Hosted Live QA {result.verdict}::{result.reason}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"\n### Verdict: {result.verdict}\n\n{result.reason}\n")
            if result.superseded_by:
                handle.write("\n| Deployment | Source | Status | Created (UTC) | Completed (UTC) |\n|---|---|---|---|---|\n")
                for row in result.superseded_by:
                    handle.write(
                        f"| `{row['id']}` | `{(row.get('source_sha') or '-')[:9]}` | {row.get('status')} "
                        f"| {row.get('created_at')} | {row.get('completed_at') or '-'} |\n"
                    )
            if result.failed_steps and result.verdict == "superseded":
                handle.write(f"\nQA steps that reported failure while the canary was moving: {', '.join(result.failed_steps)}\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"verdict={result.verdict}\n")
            handle.write(f"failed_steps={','.join(result.failed_steps)}\n")
            handle.write(f"canary_sha={result.canary_now or ''}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expected-sha", required=True, help="Commit the canary served when QA started (verified before QA).")
    parser.add_argument("--started-at", required=True, help="UTC time of that verification, ISO 8601.")
    parser.add_argument("--subdomain", required=True, help="Canary instance subdomain.")
    parser.add_argument("--own-deployment", default="", help="Deployment receipt this QA run observed, if any.")
    parser.add_argument("--step", action="append", default=[], metavar="NAME=OUTCOME", help="QA step outcome (repeatable).")
    parser.add_argument("--health-url", help="Defaults to https://<subdomain>.longhouse.ai/api/health.")
    parser.add_argument("--receipt", help="Write the verdict here as JSON (uploaded as the promotion evidence).")
    args = parser.parse_args(argv)
    if not SHA_RE.match(args.expected_sha):
        parser.error("--expected-sha must be a full 40-character commit SHA")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    outcomes = dict(item.split("=", 1) for item in args.step)
    control_plane = os.environ.get("CONTROL_PLANE_URL", "")
    token = os.environ.get("CONTROL_PLANE_ADMIN_TOKEN", "")

    def unavailable(*_args: Any) -> Any:
        raise EvidenceUnavailable("CONTROL_PLANE_URL or CONTROL_PLANE_ADMIN_TOKEN is not set")

    list_deployments, get_deployment = (
        control_plane_readers(control_plane, token) if control_plane and token else (unavailable, unavailable)
    )
    # Read first: the read can take ~25 s while the canary restarts, and a
    # deployment submitted meanwhile is part of the run.
    health = read_health(args.health_url or f"https://{args.subdomain}.longhouse.ai/api/health")
    started_at, now = parse_ts(args.started_at), datetime.now(timezone.utc)
    result = decide(
        expected_sha=args.expected_sha,
        started_at=started_at,
        now=now,
        subdomain=args.subdomain,
        own_deployment=args.own_deployment,
        step_outcomes=outcomes,
        health=health,
        list_deployments=list_deployments,
        get_deployment=get_deployment,
    )
    emit(result, args)
    if args.receipt:
        receipt = build_receipt(
            result,
            expected_sha=args.expected_sha,
            started_at=started_at,
            decided_at=now,
            subdomain=args.subdomain,
            own_deployment=args.own_deployment,
            step_outcomes=outcomes,
            image_digest=tested_image(args.own_deployment, get_deployment),
            environ=dict(os.environ),
        )
        with open(args.receipt, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
