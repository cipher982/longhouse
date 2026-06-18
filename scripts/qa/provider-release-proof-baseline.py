#!/usr/bin/env python3
"""Accept and diff provider release-proof artifacts.

This is the baseline/differential half of the provider release proof lane. It
compares normalized Longhouse contract output, while preserving raw artifacts
for debugging and review.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_BASELINE_ROOT = Path(".provider-release-proofs")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_segment(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "unknown"


def _provider(proof: dict[str, Any]) -> str:
    return str(proof.get("provider") or "unknown")


def _scenario_id(proof: dict[str, Any]) -> str:
    return str(proof.get("scenario_id") or f"{_provider(proof)}-release-proof-v1")


def _provider_version(proof: dict[str, Any]) -> str:
    return str(proof.get("provider_version") or "unknown")


def _scenario_root(root: Path, proof: dict[str, Any]) -> Path:
    return root / _provider(proof) / _scenario_id(proof)


def _version_root(root: Path, proof: dict[str, Any]) -> Path:
    return _scenario_root(root, proof) / "versions" / _safe_segment(_provider_version(proof))


def _accepted_path(root: Path, proof: dict[str, Any]) -> Path:
    return _scenario_root(root, proof) / "accepted.json"


def _load_accepted(root: Path, candidate: dict[str, Any]) -> dict[str, Any] | None:
    path = _accepted_path(root, candidate)
    try:
        return _read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None


def _copy_artifacts(
    proof: dict[str, Any],
    *,
    proof_path: Path,
    dest_dir: Path,
) -> dict[str, str]:
    archived: dict[str, str] = {}
    artifacts = proof.get("artifacts")
    if not isinstance(artifacts, dict):
        return archived
    artifacts_dir = dest_dir / "artifacts"
    for key, value in artifacts.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            continue
        source = Path(value)
        if not source.is_absolute():
            source = (proof_path.parent / source).resolve()
        if not source.is_file():
            continue
        suffix = source.suffix or ".artifact"
        dest = artifacts_dir / f"{_safe_segment(key)}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        archived[key] = str(dest)
    return archived


def accept_proof(
    proof_path: Path,
    *,
    baseline_root: Path,
) -> dict[str, Any]:
    proof_path = proof_path.expanduser().resolve()
    proof = _read_json(proof_path)
    version_dir = _version_root(baseline_root, proof)
    archived_artifacts = _copy_artifacts(
        proof,
        proof_path=proof_path,
        dest_dir=version_dir,
    )
    accepted_at = _now_iso()
    archived = dict(proof)
    archived["accepted_at"] = accepted_at
    archived["accepted_from"] = str(proof_path)
    archived["archived_artifacts"] = archived_artifacts
    archived["baseline_path"] = str(_accepted_path(baseline_root, proof))

    version_proof_path = version_dir / "proof.json"
    _write_json(version_proof_path, archived)
    _write_json(_accepted_path(baseline_root, proof), archived)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof_baseline_acceptance",
        "provider": _provider(proof),
        "provider_version": _provider_version(proof),
        "scenario_id": _scenario_id(proof),
        "accepted_at": accepted_at,
        "accepted_path": str(_accepted_path(baseline_root, proof)),
        "version_path": str(version_proof_path),
        "archived_artifacts": archived_artifacts,
    }


def _normalized(proof: dict[str, Any]) -> Any:
    normalized = proof.get("normalized")
    if not isinstance(normalized, dict):
        return normalized
    comparable = json.loads(json.dumps(normalized))
    comparable.pop("provider_version", None)
    return comparable


def _diff_normalized(base: Any, candidate: Any) -> dict[str, Any]:
    if base == candidate:
        return {"status": "match", "changes": []}
    return {
        "status": "different",
        "changes": [
            {
                "path": "$.normalized",
                "previous": base,
                "current": candidate,
            }
        ],
    }


def diff_proofs(
    candidate_path: Path,
    *,
    baseline_root: Path,
    base_path: Path | None = None,
) -> dict[str, Any]:
    candidate_path = candidate_path.expanduser().resolve()
    candidate = _read_json(candidate_path)
    if base_path is None:
        base = _load_accepted(baseline_root, candidate)
        base_uri = str(_accepted_path(baseline_root, candidate))
    else:
        base_path = base_path.expanduser().resolve()
        base = _read_json(base_path)
        base_uri = str(base_path)

    candidate_verdict = str(candidate.get("verdict") or "").lower()
    if base is None:
        if candidate_verdict == "red":
            verdict = "red"
            failure_code = str(candidate.get("failure_code") or "candidate_release_proof_failed")
            recommendation = "block_upgrade_recommendation"
        else:
            verdict = "yellow"
            failure_code = "baseline_missing"
            recommendation = "investigate_before_upgrade"
        diff = {"status": "not_compared", "changes": []}
    else:
        diff = _diff_normalized(_normalized(base), _normalized(candidate))
        if candidate_verdict == "red":
            verdict = "red"
            failure_code = str(candidate.get("failure_code") or "candidate_release_proof_failed")
            recommendation = "block_upgrade_recommendation"
        elif diff.get("status") == "match":
            verdict = "green"
            failure_code = None
            recommendation = "upgrade_allowed"
        else:
            verdict = "red"
            failure_code = "provider_release_proof_drift"
            recommendation = "block_upgrade_recommendation"

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof_diff",
        "provider": _provider(candidate),
        "provider_version": _provider_version(candidate),
        "scenario_id": _scenario_id(candidate),
        "generated_at": _now_iso(),
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": recommendation,
        "baseline": None
        if base is None
        else {
            "provider_version": base.get("provider_version"),
            "artifact_uri": base_uri,
            "accepted_at": base.get("accepted_at") or base.get("generated_at"),
        },
        "candidate": {
            "provider_version": candidate.get("provider_version"),
            "artifact_uri": str(candidate_path),
            "verdict": candidate.get("verdict"),
            "failure_code": candidate.get("failure_code"),
        },
        "diff": diff,
    }


def _print_or_write(payload: dict[str, Any], *, artifact: Path | None, as_json: bool) -> None:
    if artifact is not None:
        _write_json(artifact.expanduser(), payload)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"{payload['artifact_kind']}: {payload.get('provider')} {payload.get('verdict', 'accepted')}")
    if payload.get("failure_code"):
        print(f"failure_code: {payload['failure_code']}")
    if artifact is not None:
        print(f"artifact: {artifact}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    accept = subparsers.add_parser("accept", help="Accept a proof artifact as the current baseline")
    accept.add_argument("--proof", type=Path, required=True)
    accept.add_argument("--baseline-root", type=Path, default=DEFAULT_BASELINE_ROOT)
    accept.add_argument("--artifact", type=Path)
    accept.add_argument("--json", action="store_true")

    diff = subparsers.add_parser("diff", help="Diff a candidate proof against accepted or explicit base proof")
    diff.add_argument("--candidate", type=Path, required=True)
    diff.add_argument("--base", type=Path)
    diff.add_argument("--baseline-root", type=Path, default=DEFAULT_BASELINE_ROOT)
    diff.add_argument("--artifact", type=Path)
    diff.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "accept":
        payload = accept_proof(args.proof, baseline_root=args.baseline_root.expanduser())
        _print_or_write(payload, artifact=args.artifact, as_json=args.json)
        return 0
    if args.command == "diff":
        payload = diff_proofs(
            args.candidate,
            baseline_root=args.baseline_root.expanduser(),
            base_path=args.base,
        )
        _print_or_write(payload, artifact=args.artifact, as_json=args.json)
        return 1 if payload["verdict"] == "red" else 0
    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
