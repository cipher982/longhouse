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
COMPARABLE_ARTIFACT_KEYS = (
    "provider_contract",
    "operation_evidence",
    "session_projection",
)
STABLE_OPERATION_KEYS = ("status", "level", "canary", "failure_code")
STABLE_CHECK_KEYS = ("status", "failure_code")


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


def _accepted_path_for(root: Path, *, provider: str, scenario_id: str) -> Path:
    return root / provider / scenario_id / "accepted.json"


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
    proof_verdict = str(proof.get("verdict") or "").lower()
    if proof_verdict != "green":
        failure_code = str(proof.get("failure_code") or "baseline_acceptance_rejected")
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "provider_release_proof_baseline_acceptance",
            "provider": _provider(proof),
            "provider_version": _provider_version(proof),
            "scenario_id": _scenario_id(proof),
            "verdict": "red",
            "failure_code": "baseline_acceptance_rejected",
            "message": (
                f"Only green provider release proofs can be accepted; "
                f"candidate verdict was {proof_verdict or 'missing'} ({failure_code})."
            ),
            "accepted_path": None,
            "version_path": None,
            "archived_artifacts": {},
        }
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


def _resolve_artifact_path(value: str, *, proof_path: Path | None) -> Path:
    path = Path(value)
    if path.is_absolute() or proof_path is None:
        return path
    return (proof_path.parent / path).resolve()


def _artifact_paths(proof: dict[str, Any]) -> dict[str, str]:
    paths: dict[str, str] = {}
    artifacts = proof.get("artifacts")
    if isinstance(artifacts, dict):
        paths.update({key: value for key, value in artifacts.items() if isinstance(key, str) and isinstance(value, str)})
    archived_artifacts = proof.get("archived_artifacts")
    if isinstance(archived_artifacts, dict):
        paths.update(
            {key: value for key, value in archived_artifacts.items() if isinstance(key, str) and isinstance(value, str)}
        )
    return paths


def _artifact_json(
    proof: dict[str, Any],
    key: str,
    *,
    proof_path: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    value = _artifact_paths(proof).get(key)
    if not value:
        return None, {
            "artifact": key,
            "failure_code": "comparable_artifact_reference_missing",
            "message": f"Proof does not reference comparable artifact {key}.",
        }
    path = _resolve_artifact_path(value, proof_path=proof_path)
    try:
        return _read_json(path), None
    except FileNotFoundError:
        return None, {
            "artifact": key,
            "failure_code": "comparable_artifact_missing",
            "path": str(path),
            "message": f"Comparable artifact {key} is missing.",
        }
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return None, {
            "artifact": key,
            "failure_code": "comparable_artifact_unreadable",
            "path": str(path),
            "message": f"Comparable artifact {key} could not be read: {type(exc).__name__}: {exc}",
        }


def _stable_operation(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {}
    return {key: info.get(key) for key in STABLE_OPERATION_KEYS if info.get(key) is not None}


def _stable_check(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {}
    return {key: info.get(key) for key in STABLE_CHECK_KEYS if info.get(key) is not None}


def _stable_operation_map(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    return {
        name: _stable_operation(info)
        for name, info in sorted(payload.items())
        if isinstance(name, str) and isinstance(info, dict)
    }


def _stable_check_map(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    return {
        name: _stable_check(info)
        for name, info in sorted(payload.items())
        if isinstance(name, str) and isinstance(info, dict)
    }


def _artifact_shape_errors(key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks = {
        "provider_contract": "contract_operations",
        "operation_evidence": "operation_evidence",
    }
    required_field = checks.get(key)
    if required_field is None:
        return []
    if required_field not in payload:
        return [
            {
                "artifact": key,
                "field": required_field,
                "failure_code": "comparable_artifact_field_missing",
                "message": f"Comparable artifact {key} is missing required field {required_field}.",
            }
        ]
    if not isinstance(payload.get(required_field), dict):
        return [
            {
                "artifact": key,
                "field": required_field,
                "failure_code": "comparable_artifact_field_malformed",
                "message": f"Comparable artifact {key} field {required_field} must be an object.",
            }
        ]
    return []


def _stable_session_projection(payload: dict[str, Any]) -> dict[str, Any]:
    comparable: dict[str, Any] = {
        key: payload.get(key)
        for key in ("artifact_kind", "provider", "status")
        if payload.get(key) is not None
    }
    projection = payload.get("projection")
    if isinstance(projection, dict):
        comparable["projection"] = {
            key: projection.get(key)
            for key in ("artifact_kind", "provider", "status")
            if projection.get(key) is not None
        }
        checks = _stable_check_map(projection.get("checks"))
        if checks:
            comparable["projection"]["checks"] = checks
        operation_statuses = _stable_operation_map(projection.get("operation_statuses"))
        if operation_statuses:
            comparable["projection"]["operation_statuses"] = operation_statuses
    return comparable


def _stable_artifact(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if key == "provider_contract":
        return {
            field: payload.get(field)
            for field in ("artifact_kind", "provider", "contract_operations")
            if payload.get(field) is not None
        }
    if key == "operation_evidence":
        return {
            "artifact_kind": payload.get("artifact_kind"),
            "provider": payload.get("provider"),
            "operation_evidence": _stable_operation_map(payload.get("operation_evidence")),
        }
    if key == "session_projection":
        return _stable_session_projection(payload)
    comparable = json.loads(json.dumps(payload))
    comparable.pop("provider_version", None)
    return comparable


def _comparable_proof(
    proof: dict[str, Any],
    *,
    proof_path: Path | None,
    side: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    comparable: dict[str, Any] = {"normalized": _normalized(proof), "artifacts": {}}
    errors: list[dict[str, Any]] = []
    for key in COMPARABLE_ARTIFACT_KEYS:
        payload, error = _artifact_json(proof, key, proof_path=proof_path)
        if error is not None:
            error["side"] = side
            errors.append(error)
            comparable["artifacts"][key] = {"load_status": "unavailable"}
            continue
        if payload is not None:
            shape_errors = _artifact_shape_errors(key, payload)
            for error in shape_errors:
                error["side"] = side
            errors.extend(shape_errors)
            comparable["artifacts"][key] = _stable_artifact(key, payload)
    return comparable, errors


def _diff_comparable(base: Any, candidate: Any) -> dict[str, Any]:
    if base == candidate:
        return {"status": "match", "changes": []}
    return {
        "status": "different",
        "changes": [
            {
                "path": "$",
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
        elif candidate_verdict == "green":
            verdict = "yellow"
            failure_code = "baseline_missing"
            recommendation = "investigate_before_upgrade"
        else:
            verdict = "yellow"
            failure_code = str(candidate.get("failure_code") or "candidate_release_proof_not_green")
            recommendation = "investigate_before_upgrade"
        diff = {"status": "not_compared", "changes": []}
    else:
        base_comparable, base_errors = _comparable_proof(
            base,
            proof_path=base_path,
            side="baseline",
        )
        candidate_comparable, candidate_errors = _comparable_proof(
            candidate,
            proof_path=candidate_path,
            side="candidate",
        )
        comparable_errors = [*base_errors, *candidate_errors]
        diff = _diff_comparable(base_comparable, candidate_comparable)
        if candidate_verdict == "red":
            verdict = "red"
            failure_code = str(candidate.get("failure_code") or "candidate_release_proof_failed")
            recommendation = "block_upgrade_recommendation"
        elif candidate_verdict != "green":
            verdict = "yellow"
            failure_code = str(candidate.get("failure_code") or "candidate_release_proof_not_green")
            recommendation = "investigate_before_upgrade"
        elif comparable_errors:
            verdict = "red"
            failure_code = "provider_release_proof_comparable_artifacts_unavailable"
            recommendation = "block_upgrade_recommendation"
        elif diff.get("status") == "match":
            verdict = "green"
            failure_code = None
            recommendation = "upgrade_allowed"
        else:
            verdict = "red"
            failure_code = "provider_release_proof_drift"
            recommendation = "block_upgrade_recommendation"
        if comparable_errors:
            diff["artifact_errors"] = comparable_errors

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


def baseline_status(
    *,
    baseline_root: Path,
    provider: str,
    scenario_id: str,
) -> dict[str, Any]:
    baseline_root = baseline_root.expanduser()
    accepted_path = _accepted_path_for(
        baseline_root,
        provider=provider,
        scenario_id=scenario_id,
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof_baseline_status",
        "provider": provider,
        "scenario_id": scenario_id,
        "baseline_root": str(baseline_root),
        "accepted": False,
        "accepted_path": str(accepted_path),
        "provider_version": None,
        "accepted_at": None,
        "archived_artifacts": {},
        "missing_archived_artifacts": [],
        "verdict": "yellow",
        "failure_code": "baseline_missing",
    }
    try:
        accepted = _read_json(accepted_path)
    except FileNotFoundError:
        return payload
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload["failure_code"] = "baseline_unreadable"
        payload["message"] = f"{type(exc).__name__}: {exc}"
        return payload

    archived_artifacts = accepted.get("archived_artifacts")
    if not isinstance(archived_artifacts, dict):
        archived_artifacts = {}
    missing = [
        key
        for key, value in sorted(archived_artifacts.items())
        if not isinstance(value, str) or not Path(value).is_file()
    ]
    proof_verdict = str(accepted.get("verdict") or "").lower()
    payload.update(
        {
            "accepted": True,
            "provider_version": accepted.get("provider_version"),
            "accepted_at": accepted.get("accepted_at") or accepted.get("generated_at"),
            "archived_artifacts": archived_artifacts,
            "missing_archived_artifacts": missing,
            "verdict": "green" if proof_verdict == "green" and not missing else "yellow",
            "failure_code": None
            if proof_verdict == "green" and not missing
            else "baseline_artifacts_missing"
            if missing
            else "accepted_baseline_not_green",
        }
    )
    return payload


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

    status = subparsers.add_parser(
        "status",
        help="Inspect accepted baseline availability for one provider scenario",
    )
    status.add_argument("--provider", required=True)
    status.add_argument("--scenario-id", required=True)
    status.add_argument("--baseline-root", type=Path, default=DEFAULT_BASELINE_ROOT)
    status.add_argument("--artifact", type=Path)
    status.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "accept":
        payload = accept_proof(args.proof, baseline_root=args.baseline_root.expanduser())
        _print_or_write(payload, artifact=args.artifact, as_json=args.json)
        return 1 if payload.get("verdict") == "red" else 0
    if args.command == "diff":
        payload = diff_proofs(
            args.candidate,
            baseline_root=args.baseline_root.expanduser(),
            base_path=args.base,
        )
        _print_or_write(payload, artifact=args.artifact, as_json=args.json)
        return 1 if payload["verdict"] == "red" else 0
    if args.command == "status":
        payload = baseline_status(
            baseline_root=args.baseline_root.expanduser(),
            provider=args.provider,
            scenario_id=args.scenario_id,
        )
        _print_or_write(payload, artifact=args.artifact, as_json=args.json)
        return 1 if payload.get("verdict") == "red" else 0
    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
