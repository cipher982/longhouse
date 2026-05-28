#!/usr/bin/env python3
"""Provider release canary profile artifact generator.

This emits the shared Sauron-facing release artifact for every managed provider.
Provider-specific live canaries fill in their own evidence later; this wrapper
keeps the artifact schema and provider contract facts consistent across Codex,
Claude, OpenCode, and Antigravity.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

PROVIDER_STATUS_SCHEMA_VERSION = 1
CONTRACT_OPERATIONS = (
    "launch_local",
    "launch_remote",
    "reattach",
    "send_input",
    "interrupt",
    "steer_active_turn",
    "terminate",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
)
_GAP_OPERATION_STATUSES = {"fail", "missing", "not_run", "skipped", "stale"}


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_manifest(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "server/zerg/config/managed_provider_contracts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("managed provider manifest root must be an object")
    return payload


def _provider_contract(repo_root: Path, provider: str) -> dict[str, Any] | None:
    for item in _load_manifest(repo_root).get("providers") or []:
        if isinstance(item, dict) and item.get("provider") == provider:
            return dict(item)
    return None


def _status(status: str, **fields: Any) -> dict[str, Any]:
    data = {"status": status}
    data.update(fields)
    return data


def _fail(code: str, message: str, **fields: Any) -> dict[str, Any]:
    data = {"status": "fail", "failure_code": code, "message": message}
    data.update(fields)
    return data


def _run_version(binary: str) -> tuple[str | None, dict[str, Any] | None]:
    try:
        result = subprocess.run(
            [binary, "--version"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}
    evidence = {
        "argv": [binary, "--version"],
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    if result.returncode != 0:
        return None, evidence
    version = (result.stdout or result.stderr).strip()
    return version or None, evidence


def _resolve_binary(args: argparse.Namespace, contract: dict[str, Any]) -> str | None:
    if args.provider_bin:
        path = Path(args.provider_bin).expanduser()
        return str(path) if path.is_file() else None
    return shutil.which(str(contract["provider_cli_binary"]))


def run_binary_identity(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    if args.skip_binary_identity:
        return _status("not_run", reason="--skip-binary-identity", version=args.provider_version)

    binary = _resolve_binary(args, contract)
    if not binary:
        return _fail(
            "provider_binary_not_found",
            f"{contract['provider_cli_binary']} binary was not found on PATH",
            provider=args.provider,
        )
    version, evidence = _run_version(binary)
    if not version:
        return _fail(
            "provider_version_failed",
            "provider --version failed",
            provider=args.provider,
            path=binary,
            evidence=evidence,
        )
    return _status(
        "pass",
        provider=args.provider,
        path=binary,
        version=version,
        evidence=evidence,
    )


def run_contract_profile(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    return _status(
        "pass",
        provider=args.provider,
        managed_transport=contract["managed_transport"],
        control_plane=contract["control_plane"],
        provider_cli_binary=contract["provider_cli_binary"],
        provider_cli_env=contract.get("provider_cli_env"),
        requires_longhouse_cli=contract["requires_longhouse_cli"],
        launch_remote=contract["launch_remote"],
        send_input=contract["send_input"],
        interrupt=contract["interrupt"],
        steer_active_turn=contract["steer_active_turn"],
        reattach=contract["reattach"],
        operation_evidence=contract["operation_evidence"],
        machine_control_supports=contract["machine_control_supports"],
    )


def _manifest_operation_evidence(contract: dict[str, Any], operation: str) -> dict[str, Any]:
    evidence = (contract.get("operation_evidence") or {}).get(operation)
    return dict(evidence) if isinstance(evidence, dict) else {}


def _live_canary_profile(provider: str) -> tuple[str, str]:
    profiles = {
        "codex": (
            "codex_live_contract",
            "scripts/qa/codex-provider-release-canary.py owns Codex live bridge/TUI canaries.",
        ),
        "claude": (
            "claude_channel_live_contract",
            "longhouse provider-live canary --provider claude owns the no-token binary/auth/channel-shape canary; "
            "detached launch and active-turn steer still need live evidence.",
        ),
        "opencode": (
            "opencode_server_live_contract",
            "longhouse provider-live canary --provider opencode owns the live server schema/session/abort canary.",
        ),
        "antigravity": (
            "antigravity_real_agy_send",
            "scripts/qa/provider-control-e2e-canary.py --provider antigravity --antigravity-real-agy-send "
            "owns real agy loop-level PreInvocation hook-inbox injection proof.",
        ),
    }
    return profiles.get(provider, ("provider_live_contract", "No live canary profile registered."))


def live_canary_placeholder(provider: str) -> dict[str, Any]:
    name, reason = _live_canary_profile(provider)
    return _status("not_run", canary=name, reason=reason)


def _operation_entry(
    contract: dict[str, Any],
    operation: str,
    *,
    status: str,
    canary: str,
    level: str | None = None,
    source: str | None = None,
    message: str | None = None,
    failure_code: str | None = None,
) -> dict[str, Any]:
    target = _manifest_operation_evidence(contract, operation)
    if status in _GAP_OPERATION_STATUSES or status == "unsupported":
        effective_level = level or "none"
    else:
        effective_level = level or str(target.get("level") or "none")
    entry: dict[str, Any] = {
        "status": status,
        "level": effective_level,
        "source": source or target.get("source"),
        "canary": canary,
    }
    if failure_code:
        entry["failure_code"] = failure_code
    if message:
        entry["message"] = message
    if target.get("next"):
        entry["next"] = target.get("next")
    return {key: value for key, value in entry.items() if value is not None}


def _unsupported_operation_entry(contract: dict[str, Any], operation: str) -> dict[str, Any]:
    return _operation_entry(
        contract,
        operation,
        status="unsupported",
        canary="contract_profile",
        message="Operation is not supported by this provider contract.",
    )


def _source_review_operation_entry(
    contract: dict[str, Any],
    operation: str,
    *,
    source_review: dict[str, Any],
) -> dict[str, Any]:
    status = str(source_review.get("status") or "not_run")
    note = str(source_review.get("note") or "")
    if status in {"pass", "warn"}:
        return _operation_entry(
            contract,
            operation,
            status=status,
            canary="source_review",
            level="source_review",
            message=note,
        )
    if status == "fail":
        return _operation_entry(
            contract,
            operation,
            status="fail",
            canary="source_review",
            level="none",
            failure_code="source_review_failed",
            message=note,
        )
    return _operation_entry(
        contract,
        operation,
        status="not_run",
        canary="source_review",
        level="none",
        failure_code="insufficient_coverage",
        message=note,
    )


def build_operation_evidence(
    provider: str,
    contract: dict[str, Any],
    canaries: dict[str, dict[str, Any]],
    source_review: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    live_canary_name, live_canary_reason = _live_canary_profile(provider)
    binary_identity = canaries.get("binary_identity") or {}
    live_contract = canaries.get("live_contract") or {}
    source_review_status = str(source_review.get("status") or "not_run")

    for operation in CONTRACT_OPERATIONS:
        manifest_evidence = _manifest_operation_evidence(contract, operation)
        if not manifest_evidence and operation not in contract:
            continue
        if not bool(contract.get(operation)):
            evidence[operation] = _unsupported_operation_entry(contract, operation)
            continue

        if binary_identity.get("status") == "fail":
            evidence[operation] = _operation_entry(
                contract,
                operation,
                status="fail",
                canary="binary_identity",
                level="none",
                failure_code=binary_identity.get("failure_code"),
                message=binary_identity.get("message"),
            )
            continue

        if source_review_status == "fail":
            evidence[operation] = _source_review_operation_entry(
                contract,
                operation,
                source_review=source_review,
            )
            continue

        if str(manifest_evidence.get("level") or "") == "source_review":
            evidence[operation] = _source_review_operation_entry(
                contract,
                operation,
                source_review=source_review,
            )
            continue

        live_status = str(live_contract.get("status") or "not_run")
        if live_status == "fail":
            evidence[operation] = _operation_entry(
                contract,
                operation,
                status="fail",
                canary=str(live_contract.get("canary") or live_canary_name),
                level="none",
                failure_code=live_contract.get("failure_code") or "live_contract_failed",
                message=live_contract.get("message") or live_contract.get("reason") or live_canary_reason,
            )
        elif live_status in {"pass", "warn"}:
            evidence[operation] = _operation_entry(
                contract,
                operation,
                status=live_status,
                canary=str(live_contract.get("canary") or live_canary_name),
                source=live_contract.get("message") or live_contract.get("reason"),
            )
        else:
            evidence[operation] = _operation_entry(
                contract,
                operation,
                status="not_run",
                canary=str(live_contract.get("canary") or live_canary_name),
                level="none",
                failure_code="insufficient_coverage",
                message=live_contract.get("reason") or live_canary_reason,
            )

    return evidence


def classify_artifact(
    canaries: dict[str, dict[str, Any]],
    source_review: dict[str, Any],
) -> tuple[str, str | None, str]:
    source_status = source_review.get("status")
    if source_status == "fail":
        return "red", "source_review_failed", "block_upgrade_recommendation"
    first_not_run: str | None = None
    first_warn: str | None = None
    for name, canary in canaries.items():
        status = canary.get("status")
        if status == "fail":
            return "red", str(canary.get("failure_code") or name), "block_upgrade_recommendation"
        if status == "not_run" and first_not_run is None:
            first_not_run = name
        if status == "warn" and first_warn is None:
            first_warn = name
    if source_status in {"not_run", None} or first_not_run:
        return "yellow", "insufficient_coverage", "investigate_before_upgrade"
    if source_status == "warn" or first_warn:
        return "yellow", None, "investigate_before_upgrade"
    return "green", None, "upgrade_allowed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_root_from_script())
    parser.add_argument("--provider", required=True)
    parser.add_argument("--provider-bin")
    parser.add_argument("--provider-version")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--skip-binary-identity", action="store_true")
    parser.add_argument(
        "--source-review-status",
        choices=["not_run", "pass", "warn", "fail"],
        default="not_run",
    )
    parser.add_argument(
        "--source-review-note",
        default="Sauron source review should fill this section before publishing a release recommendation.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    contract = _provider_contract(args.repo_root, args.provider)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = args.evidence_root or args.repo_root / ".build/canaries/providers" / args.provider / timestamp
    artifact_path = args.artifact or evidence_root / "provider-release-profile.json"
    evidence_root.mkdir(parents=True, exist_ok=True)

    if contract is None:
        artifact = {
            "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
            "provider": args.provider,
            "generated_at": _now_iso(),
            "verdict": "red",
            "failure_code": "provider_contract_missing",
            "recommendation": "block_upgrade_recommendation",
            "canaries": {
                "contract_profile": _fail("provider_contract_missing", "provider is not in managed provider manifest")
            },
            "evidence_root": str(evidence_root),
        }
    else:
        canaries = {
            "contract_profile": run_contract_profile(args, contract),
            "binary_identity": run_binary_identity(args, contract),
            "live_contract": live_canary_placeholder(args.provider),
        }
        source_review = {
            "status": args.source_review_status,
            "note": args.source_review_note,
        }
        operation_evidence = build_operation_evidence(args.provider, contract, canaries, source_review)
        verdict, failure_code, recommendation = classify_artifact(canaries, source_review)
        binary_version = canaries["binary_identity"].get("version")
        artifact = {
            "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
            "provider": args.provider,
            "provider_version": args.provider_version or binary_version,
            "generated_at": _now_iso(),
            "verdict": verdict,
            "failure_code": failure_code,
            "recommendation": recommendation,
            "source_review": source_review,
            "canaries": canaries,
            "operation_evidence": operation_evidence,
            "evidence_root": str(evidence_root),
        }

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["verdict"] != "red" else 1


if __name__ == "__main__":
    raise SystemExit(main())
