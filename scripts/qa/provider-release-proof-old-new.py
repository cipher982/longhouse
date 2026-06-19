#!/usr/bin/env python3
"""Run staged old/new provider release proofs and diff the artifacts.

This is the OSS-safe bridge between "explicit old/new artifacts" and a future
private installer. Longhouse accepts already-staged provider binaries, produces
both release-proof artifacts, then delegates comparison to
provider-release-proof-baseline.py old-new.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity")
YELLOW_VERDICTS = {"yellow"}


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _default_evidence_root(repo_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return repo_root / ".build/canaries/provider-release-proof-old-new" / stamp


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _command_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "args": list(result.args) if isinstance(result.args, list) else [str(result.args)],
        "returncode": result.returncode,
    }


def _verdict_rank(verdict: str | None) -> int:
    if verdict == "red":
        return 2
    if verdict in YELLOW_VERDICTS:
        return 1
    return 0


def _max_verdict(*verdicts: str | None) -> str:
    rank = max((_verdict_rank(verdict) for verdict in verdicts), default=0)
    if rank >= 2:
        return "red"
    if rank == 1:
        return "yellow"
    return "green"


def _release_proof_argv(
    args: argparse.Namespace,
    *,
    side: str,
    provider_bin: Path,
    provider_version: str | None,
    artifact: Path,
    evidence_root: Path,
) -> list[str]:
    script = args.repo_root / "scripts" / "qa" / "provider-release-proof.py"
    argv = [
        sys.executable,
        str(script),
        "--repo-root",
        str(args.repo_root),
        "--provider",
        args.provider,
        "--provider-bin",
        str(provider_bin),
        "--artifact",
        str(artifact),
        "--evidence-root",
        str(evidence_root),
        "--source-review-status",
        args.source_review_status,
        "--source-review-note",
        args.source_review_note,
        "--timeout-secs",
        str(args.timeout_secs),
        "--scenario-id",
        f"{args.provider}-{side}-staged-release-proof-v1",
        "--json",
    ]
    if provider_version:
        argv.extend(["--provider-version", provider_version])
    if not args.skip_universal_harness:
        argv.append("--run-universal-harness")
    for scenario in args.universal_scenario or ():
        argv.extend(["--universal-scenario", scenario])
    if args.universal_fixture_path:
        argv.extend(["--universal-fixture-path", str(args.universal_fixture_path)])
    if args.universal_prompt:
        argv.extend(["--universal-prompt", args.universal_prompt])
    return argv


def _run_release_proof(
    args: argparse.Namespace,
    *,
    side: str,
    provider_bin: Path,
    provider_version: str | None,
    root: Path,
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    artifact = root / "provider-release-proof.json"
    evidence_root = root / "evidence"
    stdout_path = root / "stdout.log"
    stderr_path = root / "stderr.log"
    argv = _release_proof_argv(
        args,
        side=side,
        provider_bin=provider_bin,
        provider_version=provider_version,
        artifact=artifact,
        evidence_root=evidence_root,
    )
    try:
        result = subprocess.run(
            argv,
            cwd=str(args.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout_secs,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
        stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
        return {
            "side": side,
            "status": "fail",
            "verdict": "red",
            "failure_code": "provider_release_proof_timeout",
            "message": f"{side} provider release proof timed out after {args.timeout_secs}s",
            "artifact_path": str(artifact),
            "evidence_root": str(evidence_root),
            "provider_bin": str(provider_bin),
        }

    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    payload = _read_json(artifact)
    if payload is None:
        return {
            "side": side,
            "status": "fail",
            "verdict": "red",
            "failure_code": "provider_release_proof_missing_artifact",
            "message": f"{side} provider release proof did not write a valid artifact",
            "artifact_path": str(artifact),
            "evidence_root": str(evidence_root),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "provider_bin": str(provider_bin),
            "command": _command_evidence(result),
        }
    return {
        "side": side,
        "status": "captured" if result.returncode == 0 else "captured_red",
        "verdict": payload.get("verdict") or "red",
        "failure_code": payload.get("failure_code"),
        "artifact_path": str(artifact),
        "evidence_root": str(evidence_root),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "provider_bin": str(provider_bin),
        "provider_version": payload.get("provider_version"),
        "scenario_id": payload.get("scenario_id"),
        "command": _command_evidence(result),
    }


def _run_old_new_diff(args: argparse.Namespace, old_artifact: Path, new_artifact: Path, root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    artifact = root / "old-new-diff.json"
    stdout_path = root / "old-new-diff-stdout.log"
    stderr_path = root / "old-new-diff-stderr.log"
    script = args.repo_root / "scripts" / "qa" / "provider-release-proof-baseline.py"
    argv = [
        sys.executable,
        str(script),
        "old-new",
        "--old",
        str(old_artifact),
        "--new",
        str(new_artifact),
        "--baseline-root",
        str(args.baseline_root),
        "--artifact",
        str(artifact),
        "--json",
    ]
    result = subprocess.run(
        argv,
        cwd=str(args.repo_root),
        text=True,
        capture_output=True,
        check=False,
        timeout=args.timeout_secs,
    )
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    payload = _read_json(artifact)
    if payload is None:
        return {
            "status": "fail",
            "verdict": "red",
            "failure_code": "old_new_diff_missing_artifact",
            "artifact_path": str(artifact),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "command": _command_evidence(result),
        }
    return {
        "status": "captured" if result.returncode == 0 else "captured_red",
        "verdict": payload.get("verdict") or "red",
        "failure_code": payload.get("failure_code"),
        "artifact_path": str(artifact),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "command": _command_evidence(result),
        "summary": payload,
    }


def run_staged_old_new(args: argparse.Namespace) -> dict[str, Any]:
    evidence_root = (args.evidence_root or _default_evidence_root(args.repo_root)).expanduser()
    old = _run_release_proof(
        args,
        side="old",
        provider_bin=args.old_provider_bin.expanduser(),
        provider_version=args.old_provider_version,
        root=evidence_root / "old",
    )
    new = _run_release_proof(
        args,
        side="new",
        provider_bin=args.new_provider_bin.expanduser(),
        provider_version=args.new_provider_version,
        root=evidence_root / "new",
    )
    diff: dict[str, Any]
    if _verdict_rank(str(old.get("verdict"))) >= 2 or _verdict_rank(str(new.get("verdict"))) >= 2:
        diff = {
            "status": "blocked",
            "verdict": "red",
            "failure_code": "old_or_new_release_proof_red",
            "message": "Old/new diff skipped because at least one side did not produce a usable non-red proof.",
        }
    else:
        diff = _run_old_new_diff(
            args,
            Path(str(old["artifact_path"])),
            Path(str(new["artifact_path"])),
            evidence_root / "diff",
        )
    verdict = _max_verdict(
        str(old.get("verdict")),
        str(new.get("verdict")),
        str(diff.get("verdict")),
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof_staged_old_new",
        "generated_at": _now_iso(),
        "provider": args.provider,
        "verdict": verdict,
        "failure_code": diff.get("failure_code") if verdict == "red" else None,
        "evidence_root": str(evidence_root),
        "proofs": {"old": old, "new": new},
        "diff": diff,
        "staging": {
            "status": "staged_provider_binaries",
            "old_provider_bin": str(args.old_provider_bin.expanduser()),
            "new_provider_bin": str(args.new_provider_bin.expanduser()),
            "installer": "external",
        },
    }
    artifact = (args.artifact or (evidence_root / "provider-release-proof-staged-old-new.json")).expanduser()
    payload["artifact_path"] = str(artifact)
    _write_json(artifact, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_root_from_script())
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, required=True)
    parser.add_argument("--old-provider-bin", type=Path, required=True)
    parser.add_argument("--new-provider-bin", type=Path, required=True)
    parser.add_argument("--old-provider-version")
    parser.add_argument("--new-provider-version")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--baseline-root", type=Path, default=Path(".build/provider-release-baselines"))
    parser.add_argument("--timeout-secs", type=int, default=180)
    parser.add_argument("--skip-universal-harness", action="store_true")
    parser.add_argument(
        "--universal-scenario",
        action="append",
        help="Universal scenario passed through to both provider-release-proof runs.",
    )
    parser.add_argument("--universal-fixture-path", type=Path)
    parser.add_argument("--universal-prompt")
    parser.add_argument(
        "--source-review-status",
        choices=["not_run", "pass", "warn", "fail"],
        default="not_run",
    )
    parser.add_argument(
        "--source-review-note",
        default="Staged old/new proof did not include external source-review evidence.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.expanduser().resolve()
    args.baseline_root = args.baseline_root.expanduser()
    payload = run_staged_old_new(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"verdict: {payload['verdict']}")
        print(f"artifact: {payload['artifact_path']}")
    return 1 if payload.get("verdict") == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
