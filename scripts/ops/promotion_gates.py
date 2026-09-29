#!/usr/bin/env python3
"""The hard gates of a production promotion, and the receipt of what they read.

`make promote-production` runs this before anything moves (release-rings.md,
change 2). Production takes the exact image dogfood is serving, so every gate is
about one (commit, digest) pair, and none is satisfied by a summary that merely
looks green:

  dogfood        dogfood's /api/health serves the commit, and the control plane
                 records that dogfood is running the promoted digest right now
                 (not that it once did).
  hosted_qa      a completed Hosted Live QA run on that exact commit recorded the
                 verdict `passed`. A superseded run also concludes success, so it
                 is not evidence, and neither is a failed or unfinished one.
  engine_compat  the previous released engine shipped a transcript to this
                 commit's server and it was served (the receipt says `passed`; a
                 skip is not a pass).
  soak           the control plane, not this script, decides whether the 24 h
                 soak applies (any real tenant) and whether the image has run on
                 dogfood that long. A status that cannot be read is a refusal.

Unknown is never a pass: a read that fails, a receipt that is missing or
malformed, or a field that is absent refuses the gate that needed it. Every gate
runs, so one check lists everything that is missing; the receipt (stdout, JSON)
carries the evidence each gate used or the reason it refused. Exit 0 only when
every gate passed and the target list could be read.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Callable

RECEIPT_SCHEMA = "longhouse.production-promotion-receipt.v1"
QA_SCHEMA = "longhouse.hosted-qa-verdict.v1"
QA_MEMBER = "hosted-live-qa-verdict.json"
QA_WORKFLOW = "Hosted Live QA"
COMPAT_SCHEMA = "longhouse.engine-compat.v1"
COMPAT_MEMBER = "receipt.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
GATES = ("dogfood", "hosted_qa", "engine_compat", "soak")


class SourceError(Exception):
    """A read could not be completed; the gate that needed it refuses."""


class Refusal(Exception):
    """A gate's reason for saying no."""


@dataclass(frozen=True)
class Config:
    repo: str
    dogfood_subdomain: str
    dogfood_health_url: str


class Sources:
    """Every read a gate makes. Tests substitute fixtures for this."""

    def health(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    def control(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def github(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def artifact_zip(self, repo: str, artifact_id: int) -> bytes:
        raise NotImplementedError


def _short(sha: str) -> str:
    return sha[:9]


# --- gate 1: dogfood -------------------------------------------------------------------------------------------------


def gate_dogfood(cfg: Config, sources: Sources, sha: str) -> dict[str, Any]:
    """Dogfood serves `sha` and the control plane says it runs the digest it will promote."""
    subdomain = cfg.dogfood_subdomain
    try:
        health = sources.health(cfg.dogfood_health_url)
    except SourceError as exc:
        raise Refusal(f"dogfood ({subdomain}) health is unreadable: {exc}") from exc
    build = health.get("build") if isinstance(health.get("build"), dict) else {}
    if health.get("status") != "healthy":
        raise Refusal(f"dogfood ({subdomain}) is not healthy right now (status={health.get('status')!r})")
    served = build.get("commit")
    if served != sha:
        raise Refusal(
            f"dogfood ({subdomain}) serves {served or 'an unknown commit'}, not {sha}: "
            f"run `make promote-dogfood SHA={sha}` first"
        )
    if build.get("dirty") is not False:
        raise Refusal(f"dogfood ({subdomain}) reports a dirty or unlabeled build of {_short(sha)}")

    key = f"promote-dogfood-{subdomain}-{sha}"
    try:
        rows = sources.control("/api/deployments", {"submission_key": key, "limit": "50"}).get("deployments") or []
    except SourceError as exc:
        raise Refusal(f"cannot read dogfood deployments from the control plane: {exc}") from exc
    done = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("status") == "success"
        and row.get("source_sha") == sha
        and row.get("completed_at")
        and DIGEST_RE.match(str(row.get("image_digest") or ""))
    ]
    if not done:
        raise Refusal(f"no successful dogfood deployment of {_short(sha)} (submission_key {key}); promote-dogfood it first")
    deployment = max(done, key=lambda row: str(row["completed_at"]))
    digest = deployment["image_digest"]

    try:
        detail = sources.control(f"/api/deployments/{urllib.parse.quote(str(deployment['id']), safe='')}")
    except SourceError as exc:
        raise Refusal(f"cannot read dogfood deployment {deployment['id']}: {exc}") from exc
    instance_id = next(
        (t.get("id") for t in detail.get("targets") or [] if isinstance(t, dict) and t.get("subdomain") == subdomain), None
    )
    row = next(
        (e for e in detail.get("target_evidence") or [] if isinstance(e, dict) and e.get("instance_id") == instance_id), None
    )
    if instance_id is None or row is None:
        raise Refusal(f"dogfood deployment {deployment['id']} has no target evidence for {subdomain}")
    if row.get("disposition") != "success":
        raise Refusal(f"dogfood deployment {deployment['id']} ended {row.get('disposition')!r} for {subdomain}")
    current = row.get("instance_current_image")
    if current != digest:
        raise Refusal(
            f"the control plane records dogfood ({subdomain}) running {current or 'an unknown image'}, not the promoted {digest}"
        )
    if row.get("instance_applied_generation") != row.get("instance_desired_generation"):
        raise Refusal(
            f"dogfood ({subdomain}) has not finished applying: generation "
            f"{row.get('instance_applied_generation')} of {row.get('instance_desired_generation')}"
        )
    return {
        "served_commit": served,
        "image_digest": digest,
        "deployment_id": deployment["id"],
        "completed_at": deployment["completed_at"],
        "instance_current_image": current,
        "instance_generation": row.get("instance_applied_generation"),
    }


# --- gates 2 and 3: receipts CI recorded per commit -------------------------------------------------------------------


def _receipts(cfg: Config, sources: Sources, name: str, member: str) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int]:
    """(artifact, receipt) newest first, and how many artifacts could not be read."""
    try:
        listing = sources.github(f"repos/{cfg.repo}/actions/artifacts", {"name": name, "per_page": "100"})
    except SourceError as exc:
        raise Refusal(f"cannot list GitHub artifacts named {name}: {exc}") from exc
    artifacts = [a for a in listing.get("artifacts") or [] if isinstance(a, dict) and a.get("name") == name and not a.get("expired")]
    artifacts.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unreadable = 0
    for artifact in artifacts:
        try:
            raw = sources.artifact_zip(cfg.repo, int(artifact["id"]))
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                receipt = json.loads(archive.read(member))
        except (SourceError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            unreadable += 1
            print(f"warning: unreadable {name} artifact {artifact.get('id')}: {exc}", file=sys.stderr)
            continue
        if isinstance(receipt, dict):
            found.append((artifact, receipt))
        else:
            unreadable += 1
    return found, unreadable


def gate_hosted_qa(cfg: Config, sources: Sources, sha: str, digest: str | None) -> dict[str, Any]:
    """The newest decisive Hosted Live QA verdict for this exact commit is `passed`, from a completed run."""
    name = f"hosted-live-qa-verdict-{sha}"
    found, unreadable = _receipts(cfg, sources, name, QA_MEMBER)
    if not found:
        raise Refusal(
            f"no Hosted Live QA verdict receipt for {_short(sha)} (artifact {name}"
            f"{f', {unreadable} unreadable' if unreadable else ''}); QA has not run on this commit since receipts began, "
            f"or its artifact expired. QA runs against the canary, so redeploy it to this commit, which dispatches QA: "
            f"gh workflow run deploy-and-verify.yml --ref main -f runtime_image_tag={sha}"
        )
    decisive: list[tuple[dict[str, Any], dict[str, Any]]] = []
    superseded = 0
    for artifact, receipt in found:
        if receipt.get("schema") != QA_SCHEMA or receipt.get("verified_sha") != sha:
            continue
        if receipt.get("verdict") == "superseded":
            superseded += 1
        else:
            decisive.append((artifact, receipt))
    if not decisive:
        raise Refusal(
            f"Hosted Live QA has no decisive run for {_short(sha)}: {superseded} superseded "
            f"(the canary was replaced mid-run; a superseded run is not qualification). Re-run it on this commit."
        )
    artifact, receipt = decisive[0]
    if receipt.get("verdict") != "passed":
        raise Refusal(
            f"the latest Hosted Live QA verdict for {_short(sha)} is {receipt.get('verdict')!r}: {receipt.get('reason')}"
        )
    if receipt.get("canary_sha") != sha or receipt.get("failed_steps"):
        raise Refusal(f"the Hosted Live QA receipt for {_short(sha)} says passed but is partial: {receipt.get('failed_steps')}")
    run_id = (artifact.get("workflow_run") or {}).get("id")
    if run_id is None or str((receipt.get("run") or {}).get("id")) != str(run_id):
        raise Refusal(f"the Hosted Live QA receipt does not belong to the run that uploaded it (artifact run {run_id})")
    try:
        run = sources.github(f"repos/{cfg.repo}/actions/runs/{run_id}")
    except SourceError as exc:
        raise Refusal(f"cannot read Hosted Live QA run {run_id}: {exc}") from exc
    if run.get("name") != QA_WORKFLOW or run.get("status") != "completed" or run.get("conclusion") != "success":
        raise Refusal(
            f"Hosted Live QA run {run_id} is not a completed successful run "
            f"(workflow={run.get('name')!r} status={run.get('status')!r} conclusion={run.get('conclusion')!r})"
        )
    tested = receipt.get("canary_image_digest")
    if digest and tested and tested != digest:
        raise Refusal(f"Hosted Live QA tested {tested}, not the promoted {digest}")
    return {
        "run_id": run_id,
        "run_url": run.get("html_url"),
        "verdict": "passed",
        "verified_sha": receipt["verified_sha"],
        "decided_at": receipt.get("decided_at"),
        "canary_deployment_id": receipt.get("canary_deployment_id"),
        "canary_image_digest": tested,
        "superseded_runs_ignored": superseded,
    }


def gate_engine_compat(cfg: Config, sources: Sources, sha: str) -> dict[str, Any]:
    """The previous released engine shipped to this commit's server and was served."""
    name = f"engine-compat-{sha}"
    found, unreadable = _receipts(cfg, sources, name, COMPAT_MEMBER)
    found = [
        (a, r)
        for a, r in found
        if r.get("schema") == COMPAT_SCHEMA
        and r.get("source_sha") == sha
        and (a.get("workflow_run") or {}).get("head_sha") in (None, sha)
    ]
    if not found:
        raise Refusal(
            f"no engine-compat receipt for {_short(sha)} (artifact {name}"
            f"{f', {unreadable} unreadable' if unreadable else ''}): the previous-release engine smoke has not "
            f"reported on this commit; it is the CI job 'Engine compatibility (previous release)'"
        )
    artifact, receipt = found[0]
    if receipt.get("result") != "passed":
        raise Refusal(
            f"engine-compat for {_short(sha)} is {receipt.get('result')!r}, not passed: {receipt.get('reason') or 'no reason recorded'}"
        )
    return {
        "run_id": (artifact.get("workflow_run") or {}).get("id"),
        "previous_release_tag": receipt.get("previous_release_tag"),
        "previous_engine": receipt.get("previous_engine"),
        "counts": receipt.get("counts"),
        "recorded_at": receipt.get("recorded_at"),
        "subject": receipt.get("subject"),
    }


# --- gate 4: soak ---------------------------------------------------------------------------------------------------


def gate_soak(sources: Sources, digest: str | None) -> dict[str, Any]:
    """The control plane says the soak, if one applies, has elapsed for this image."""
    if not digest:
        raise Refusal("not evaluated: there is no digest to soak until the dogfood gate passes")
    try:
        state = sources.control("/api/deployments/production-soak", {"image": digest})
    except SourceError as exc:
        raise Refusal(
            f"cannot read tenant status from the control plane ({exc}); unknown is never read as pre-launch, "
            f"so the soak is treated as required and unsatisfied"
        ) from exc
    hours = state.get("required_hours")
    tenants = state.get("real_tenants")
    if not isinstance(hours, int) or isinstance(hours, bool) or not isinstance(tenants, list) or not isinstance(state.get("satisfied"), bool):
        raise Refusal(f"the control plane's production-soak answer is malformed: {json.dumps(state)[:300]}")
    who = [str(t.get("subdomain")) for t in tenants if isinstance(t, dict)]
    if state["satisfied"] is not True:
        when = f"; the earliest dogfood run of this image is ready {state['earliest_ready_at']}" if state.get("earliest_ready_at") else ""
        raise Refusal(
            f"the {hours}h soak is required (real tenants: {', '.join(who) or 'none'}; paid signups in progress: "
            f"{state.get('pending_paid_intents')}) and this image has not run on dogfood that long{when}"
        )
    return {
        "required_hours": hours,
        "enforced": bool(state.get("enforced")),
        "real_tenants": who,
        "pending_paid_intents": state.get("pending_paid_intents"),
        "soaked_by": state.get("soaked_by"),
    }


# --- the plan: which tenants the wave touches ---------------------------------------------------------------------------


def plan_targets(cfg: Config, sources: Sources) -> dict[str, Any]:
    """Every active hosted tenant except dogfood; none means a pointer-only promotion."""
    try:
        instances = sources.control("/api/instances").get("instances")
    except SourceError as exc:
        raise Refusal(f"cannot list control-plane instances: {exc}") from exc
    if not isinstance(instances, list):
        raise Refusal("the control plane's instance list is malformed")
    targets = sorted(
        (
            {"id": int(inst["id"]), "subdomain": inst.get("subdomain")}
            for inst in instances
            if isinstance(inst, dict) and inst.get("status") == "active" and inst.get("subdomain") != cfg.dogfood_subdomain
        ),
        key=lambda target: target["id"],
    )
    return {"targets": targets, "pointer_only": not targets}


# --- evaluation -----------------------------------------------------------------------------------------------------


def _run(record: dict[str, Any], name: str, gate: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
    try:
        evidence = gate()
    except Refusal as refusal:
        record[name] = {"ok": False, "refusal": str(refusal)}
        return None
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # Evidence in a shape no gate expects is missing evidence, not a crash.
        record[name] = {"ok": False, "refusal": f"malformed evidence ({type(exc).__name__}: {exc})"}
        return None
    record[name] = {"ok": True, "evidence": evidence}
    return evidence


def evaluate(cfg: Config, sources: Sources, sha: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Run every gate for `sha` (default: the commit dogfood serves) and return the receipt."""
    gates: dict[str, Any] = {}
    if sha is None:
        try:
            build = sources.health(cfg.dogfood_health_url).get("build")
            sha = (build or {}).get("commit") if isinstance(build, dict) else None
        except SourceError:
            sha = None
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            gates["dogfood"] = {"ok": False, "refusal": f"cannot resolve the commit dogfood ({cfg.dogfood_subdomain}) serves; pass SHA="}
            return _receipt(cfg, None, None, gates, None, now)
    dogfood = _run(gates, "dogfood", lambda: gate_dogfood(cfg, sources, sha))
    digest = dogfood["image_digest"] if dogfood else None
    _run(gates, "hosted_qa", lambda: gate_hosted_qa(cfg, sources, sha, digest))
    _run(gates, "engine_compat", lambda: gate_engine_compat(cfg, sources, sha))
    _run(gates, "soak", lambda: gate_soak(sources, digest))
    plan: dict[str, Any] | None
    try:
        plan = plan_targets(cfg, sources)
    except Refusal as refusal:
        plan = {"ok": False, "refusal": str(refusal)}
    return _receipt(cfg, sha, digest, gates, plan, now)


def _receipt(
    cfg: Config, sha: str | None, digest: str | None, gates: dict[str, Any], plan: dict[str, Any] | None, now: datetime | None
) -> dict[str, Any]:
    promotable = all(gates.get(name, {}).get("ok") for name in GATES) and plan is not None and "refusal" not in plan
    return {
        "schema": RECEIPT_SCHEMA,
        "checked_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": cfg.repo,
        "dogfood_subdomain": cfg.dogfood_subdomain,
        "sha": sha,
        "image_digest": digest,
        "promotable": promotable,
        "gates": gates,
        "plan": plan,
    }


def refusals(receipt: dict[str, Any]) -> list[str]:
    lines = [f"gate {name}: {gate['refusal']}" for name, gate in receipt["gates"].items() if not gate.get("ok")]
    plan = receipt.get("plan")
    if plan is not None and "refusal" in plan:
        lines.append(f"plan: {plan['refusal']}")
    return lines


# --- the real world --------------------------------------------------------------------------------------------------


class LiveSources(Sources):
    def __init__(self, control_plane_url: str, admin_token: str) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")
        self.admin_token = admin_token

    @staticmethod
    def _get_json(url: str, headers: dict[str, str], *, attempts: int = 3) -> dict[str, Any]:
        last = "no attempt made"
        for attempt in range(attempts):
            # Cloudflare answers the default Python-urllib agent with 403 (error 1010).
            request = urllib.request.Request(url, headers={"User-Agent": "longhouse-promotion-gates/1", **headers})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https URLs from operator config
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise SourceError(f"{url} returned {type(payload).__name__}, not an object")
                return payload
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise SourceError(f"{url} answered HTTP {exc.code}") from exc
                last = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = str(getattr(exc, "reason", exc))
            except ValueError as exc:
                raise SourceError(f"{url} did not return JSON: {exc}") from exc
            if attempt + 1 < attempts:
                time.sleep(2)
        raise SourceError(f"{url} unreachable after {attempts} attempts ({last})")

    def health(self, url: str) -> dict[str, Any]:
        return self._get_json(url, {})

    def control(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._get_json(f"{self.control_plane_url}{path}{query}", {"X-Admin-Token": self.admin_token})

    @staticmethod
    def _gh(args: list[str], *, attempts: int = 2) -> bytes:
        last = ""
        for attempt in range(attempts):
            try:
                proc = subprocess.run(["gh", "api", *args], capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SourceError(f"gh api {args[0]}: {exc}") from exc
            if proc.returncode == 0:
                return proc.stdout
            last = proc.stderr.decode(errors="replace").strip()[:300]
            if attempt + 1 < attempts:
                time.sleep(2)
        raise SourceError(f"gh api {args[0]} failed: {last}")

    def github(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        args = ["-X", "GET", path]
        for key, value in (params or {}).items():
            args += ["-f", f"{key}={value}"]
        try:
            payload = json.loads(self._gh(args))
        except ValueError as exc:
            raise SourceError(f"gh api {path} did not return JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"gh api {path} returned {type(payload).__name__}, not an object")
        return payload

    def artifact_zip(self, repo: str, artifact_id: int) -> bytes:
        return self._gh([f"repos/{repo}/actions/artifacts/{artifact_id}/zip"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", help="Exact 40-character commit. Default: the commit dogfood serves right now.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "cipher982/longhouse"))
    parser.add_argument("--dogfood-subdomain", required=True)
    parser.add_argument("--dogfood-health-url", help="Default: https://<subdomain>.longhouse.ai/api/health")
    args = parser.parse_args(argv)
    if args.sha is not None and not SHA_RE.match(args.sha):
        parser.error("--sha must be an exact 40-character lowercase commit SHA (omit it to promote what dogfood serves)")
    control_plane = os.environ.get("CONTROL_PLANE_URL", "")
    token = os.environ.get("CONTROL_PLANE_ADMIN_TOKEN", "")
    if not control_plane or not token:
        parser.error("CONTROL_PLANE_URL and CONTROL_PLANE_ADMIN_TOKEN are required")
    cfg = Config(
        repo=args.repo,
        dogfood_subdomain=args.dogfood_subdomain,
        dogfood_health_url=args.dogfood_health_url or f"https://{args.dogfood_subdomain}.longhouse.ai/api/health",
    )
    receipt = evaluate(cfg, LiveSources(control_plane, token), args.sha)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    for line in refusals(receipt):
        print(f"REFUSED {line}", file=sys.stderr)
    return 0 if receipt["promotable"] else 1


if __name__ == "__main__":
    sys.exit(main())
