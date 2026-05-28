"""Managed provider support-state projection.

This read model joins separate facts without collapsing them:

- contract capability: what Longhouse implements for a provider
- proof maturity: how strongly each supported operation is verified
- version readiness: whether release-drift evidence applies to the installed CLI
- local live proof: whether this machine has proven operations for the current CLI

Provider-specific code still owns execution. This module only gives local
health and doctor surfaces a single, explicit projection to display.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zerg.services.managed_provider_contracts import all_managed_provider_contracts

SCHEMA_VERSION = 1
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
EVIDENCE_RANK = {
    "none": 0,
    "source_review": 1,
    "hermetic": 2,
    "live_no_token": 3,
    "manual_live_token": 4,
    "scheduled_live_token": 5,
}
_LEVEL_BY_RANK = {rank: level for level, rank in EVIDENCE_RANK.items()}
_RELEASE_GAP_STATUSES = {"fail", "missing", "not_run", "skipped", "stale"}


def collect_provider_support_state(
    *,
    provider_clis: Mapping[str, Any] | None,
    provider_release_status: Mapping[str, Any] | None,
    provider_live_proof: Mapping[str, Any] | None = None,
    control_channel: Mapping[str, Any] | None,
) -> dict[str, Any]:
    provider_clis = dict(provider_clis or {})
    provider_release_status = dict(provider_release_status or {})
    provider_live_proof = dict(provider_live_proof or {})
    control_channel = dict(control_channel or {})
    release_statuses = dict(provider_release_status.get("statuses") or {})
    live_proof_statuses = dict(provider_live_proof.get("statuses") or {})
    live_ops_by_provider = dict(control_channel.get("control_operations_by_provider") or {})
    raw_control_status = str(control_channel.get("status") or "").strip()
    control_connected = raw_control_status == "connected"

    providers: dict[str, Any] = {}
    for contract in all_managed_provider_contracts():
        provider = contract.provider
        cli_info = dict(provider_clis.get(provider) or {})
        release_info = dict(release_statuses.get(provider) or {})
        live_proof_info = dict(live_proof_statuses.get(provider) or {})
        live_control_operations = tuple(str(item) for item in live_ops_by_provider.get(provider) or ())
        missing_live_control_operations = _missing_live_control_operations(
            expected_supports=contract.machine_control_supports,
            live_control_operations=live_control_operations,
        )
        operations = _operation_states(contract, release_info=release_info, live_proof_info=live_proof_info)
        version_readiness = _version_readiness(release_info)
        proof = _proof_summary(operations, live_proof_info=live_proof_info)
        provider_state = _provider_state(
            cli_info=cli_info,
            contract_requires_cli=bool(getattr(contract, "launch_local", False)),
            version_readiness=version_readiness,
            proof=proof,
            control_connected=control_connected,
            expected_supports=contract.machine_control_supports,
            live_control_operations=live_control_operations,
            missing_live_control_operations=missing_live_control_operations,
        )
        providers[provider] = {
            "provider": provider,
            "state": provider_state,
            "managed_transport": contract.managed_transport.value,
            "control_plane": contract.control_plane,
            "cli": {
                "state": _cli_state(cli_info),
                "path": cli_info.get("path"),
                "source": cli_info.get("source"),
                "resolution_error": cli_info.get("resolution_error"),
            },
            "capabilities": {
                "supported_operations": _operation_names_by_support(operations, supported=True),
                "unsupported_operations": _operation_names_by_support(operations, supported=False),
                "machine_control_supports": list(contract.machine_control_supports),
                "live_control_operations": list(live_control_operations),
                "missing_live_control_operations": list(missing_live_control_operations),
                "live_control_state": _live_control_state(
                    control_connected=control_connected,
                    expected_supports=contract.machine_control_supports,
                    live_control_operations=live_control_operations,
                    missing_live_control_operations=missing_live_control_operations,
                ),
            },
            "proof": proof,
            "operations": operations,
            "version_readiness": version_readiness,
            "live_proof": _live_proof_summary(live_proof_info),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "summary": _summary(providers),
        "providers": providers,
    }


def _operation_states(
    contract: Any,
    *,
    release_info: Mapping[str, Any],
    live_proof_info: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    release_operation_evidence = dict(release_info.get("operation_evidence") or {})
    live_proof_operation_evidence = dict(live_proof_info.get("operation_evidence") or {})
    live_proof_applies = bool(live_proof_info.get("applies"))
    operations: dict[str, dict[str, Any]] = {}
    for operation in CONTRACT_OPERATIONS:
        supported = bool(getattr(contract, operation, False))
        manifest_evidence = dict(contract.operation_evidence_for(operation))
        target_level = str(manifest_evidence.get("level") or "none")
        target_rank = EVIDENCE_RANK.get(target_level, -1)
        release_evidence = dict(release_operation_evidence.get(operation) or {})
        raw_local_proof_evidence = dict(live_proof_operation_evidence.get(operation) or {})
        local_proof_evidence = raw_local_proof_evidence if live_proof_applies else {}
        effective_evidence, effective_source = _select_effective_evidence(
            release_evidence=release_evidence,
            local_proof_evidence=local_proof_evidence,
        )
        evidence_level, evidence_rank = _effective_evidence_level(
            target_level=target_level,
            target_rank=target_rank,
            evidence=effective_evidence,
        )
        operations[operation] = {
            "supported": supported,
            "evidence_level": evidence_level,
            "evidence_rank": evidence_rank,
            "target_evidence_level": target_level,
            "target_evidence_rank": target_rank,
            "evidence_source": effective_evidence.get("source") or manifest_evidence.get("source"),
            "evidence_origin": effective_source,
            "manifest_evidence_source": manifest_evidence.get("source"),
            "release_evidence": _release_evidence_summary(release_evidence, release_info=release_info),
            "local_proof_evidence": _local_proof_evidence_summary(
                raw_local_proof_evidence,
                live_proof_info=live_proof_info,
            ),
            "evidence_state": _evidence_state(effective_evidence, origin=effective_source),
            "next": local_proof_evidence.get("next") or release_evidence.get("next") or manifest_evidence.get("next"),
        }
    return operations


def _effective_evidence_level(
    *,
    target_level: str,
    target_rank: int,
    evidence: Mapping[str, Any],
) -> tuple[str, int]:
    evidence_level = str(evidence.get("level") or "").strip()
    if evidence_level in EVIDENCE_RANK:
        return evidence_level, EVIDENCE_RANK[evidence_level]
    evidence_status = str(evidence.get("status") or "").strip()
    if evidence_status in _RELEASE_GAP_STATUSES:
        return "none", EVIDENCE_RANK["none"]
    return target_level, target_rank


def _evidence_status(evidence: Mapping[str, Any]) -> str:
    return str(evidence.get("status") or "").strip()


def _evidence_rank(evidence: Mapping[str, Any]) -> int:
    level = str(evidence.get("level") or "").strip()
    return EVIDENCE_RANK.get(level, -1)


def _is_gap_or_failure(evidence: Mapping[str, Any]) -> bool:
    return _evidence_status(evidence) in _RELEASE_GAP_STATUSES


def _select_effective_evidence(
    *,
    release_evidence: Mapping[str, Any],
    local_proof_evidence: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    if local_proof_evidence and _is_gap_or_failure(local_proof_evidence):
        return local_proof_evidence, "local_proof"
    if release_evidence and _is_gap_or_failure(release_evidence):
        return release_evidence, "release"
    if local_proof_evidence and release_evidence:
        if _evidence_rank(local_proof_evidence) >= _evidence_rank(release_evidence):
            return local_proof_evidence, "local_proof"
        return release_evidence, "release"
    if local_proof_evidence:
        return local_proof_evidence, "local_proof"
    if release_evidence:
        return release_evidence, "release"
    return {}, "manifest"


def _release_evidence_summary(
    release_evidence: Mapping[str, Any],
    *,
    release_info: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not release_evidence:
        return None
    return {
        "status": release_evidence.get("status"),
        "level": release_evidence.get("level"),
        "source": release_evidence.get("source"),
        "failure_code": release_evidence.get("failure_code")
        or (release_info.get("failure_code") if _is_gap_or_failure(release_evidence) else None),
        "message": release_evidence.get("message"),
        "generated_at": release_evidence.get("generated_at") or release_info.get("generated_at"),
        "canary": release_evidence.get("canary"),
        "canaries": release_evidence.get("canaries"),
    }


def _local_proof_evidence_summary(
    local_proof_evidence: Mapping[str, Any],
    *,
    live_proof_info: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not local_proof_evidence:
        return None
    return {
        "status": local_proof_evidence.get("status"),
        "level": local_proof_evidence.get("level"),
        "source": local_proof_evidence.get("source"),
        "failure_code": local_proof_evidence.get("failure_code")
        or (live_proof_info.get("failure_code") if _is_gap_or_failure(local_proof_evidence) else None),
        "message": local_proof_evidence.get("message"),
        "generated_at": local_proof_evidence.get("generated_at") or live_proof_info.get("generated_at"),
        "canary": local_proof_evidence.get("canary"),
        "canaries": local_proof_evidence.get("canaries"),
    }


def _evidence_state(evidence: Mapping[str, Any], *, origin: str) -> str:
    if origin == "manifest" or not evidence:
        return "manifest_only"
    status = str(evidence.get("status") or "").strip()
    prefix = "local_proof" if origin == "local_proof" else "release"
    if status == "pass":
        return f"{prefix}_proven"
    if status == "fail":
        return f"{prefix}_failed"
    if status in _RELEASE_GAP_STATUSES:
        return f"{prefix}_gap"
    return f"{prefix}_unknown"


def _operation_names_by_support(operations: Mapping[str, Mapping[str, Any]], *, supported: bool) -> list[str]:
    return [operation for operation, info in operations.items() if bool(info.get("supported")) == supported]


def _expected_live_control_operations(expected_supports: tuple[str, ...]) -> tuple[str, ...]:
    operations: list[str] = []
    seen: set[str] = set()
    for support in expected_supports:
        _, _, operation = str(support).partition(".")
        if operation and operation not in seen:
            operations.append(operation)
            seen.add(operation)
    return tuple(operations)


def _missing_live_control_operations(
    *,
    expected_supports: tuple[str, ...],
    live_control_operations: tuple[str, ...],
) -> tuple[str, ...]:
    live = {str(operation) for operation in live_control_operations}
    expected = _expected_live_control_operations(expected_supports)
    return tuple(operation for operation in expected if operation not in live)


def _proof_summary(
    operations: Mapping[str, Mapping[str, Any]],
    *,
    live_proof_info: Mapping[str, Any],
) -> dict[str, Any]:
    supported = {operation: dict(info) for operation, info in operations.items() if info.get("supported")}
    if not supported:
        return {
            "state": "unsupported",
            "minimum_evidence_level": "none",
            "minimum_evidence_operations": [],
        }

    minimum_rank = min(int(info.get("evidence_rank", -1)) for info in supported.values())
    minimum_level = next((level for level, rank in EVIDENCE_RANK.items() if rank == minimum_rank), "unknown")
    minimum_operations = []
    for operation, info in sorted(supported.items()):
        if int(info.get("evidence_rank", -1)) == minimum_rank:
            minimum_operations.append(operation)
    release_failed_operations = []
    local_proof_failed_operations = []
    release_gap_operations = []
    local_proof_gap_operations = []
    for operation, info in sorted(supported.items()):
        evidence_state = info.get("evidence_state")
        if evidence_state == "release_failed":
            release_failed_operations.append(operation)
        elif evidence_state == "local_proof_failed":
            local_proof_failed_operations.append(operation)
        elif evidence_state == "release_gap":
            release_gap_operations.append(operation)
        elif evidence_state == "local_proof_gap":
            local_proof_gap_operations.append(operation)
    local_proof_verdict = str(live_proof_info.get("verdict") or "").lower()
    local_proof_verdict_failed = bool(live_proof_info.get("applies")) and local_proof_verdict == "red"
    # Keep release failures dominant, then local failures, then incomplete
    # evidence. Passing evidence is ranked earlier per operation.
    if release_failed_operations:
        state = "release_failed"
    elif local_proof_failed_operations:
        state = "local_proof_failed"
    elif local_proof_verdict_failed:
        state = "local_proof_failed"
    elif release_gap_operations:
        state = "release_incomplete"
    elif local_proof_gap_operations:
        state = "local_proof_incomplete"
    elif minimum_rank >= EVIDENCE_RANK["scheduled_live_token"]:
        state = "scheduled_live_token"
    else:
        state = "mixed"
    return {
        "state": state,
        "minimum_evidence_level": minimum_level,
        "minimum_evidence_operations": minimum_operations,
        "release_failed_operations": release_failed_operations,
        "release_gap_operations": release_gap_operations,
        "local_proof_failed_operations": local_proof_failed_operations,
        "local_proof_gap_operations": local_proof_gap_operations,
        "local_proof_verdict_failed": local_proof_verdict_failed,
    }


def _cli_state(cli_info: Mapping[str, Any]) -> str:
    if cli_info.get("path"):
        return "available"
    if cli_info.get("resolution_error"):
        return "missing"
    return "unknown"


def _version_readiness(release_info: Mapping[str, Any]) -> dict[str, Any]:
    status = str(release_info.get("status") or "not_configured")
    risk = str(release_info.get("risk") or "none")
    if risk == "blocking":
        state = "blocked_installed_release"
    elif risk == "warning":
        state = "installed_release_needs_attention"
    elif status == "candidate_newer_than_local":
        state = "candidate_release_pending_review"
    elif status == "ok":
        state = "installed_release_reviewed"
    elif status == "no_artifact":
        state = "no_artifact"
    elif status == "not_configured":
        state = "not_configured"
    else:
        state = status
    return {
        "state": state,
        "status": status,
        "risk": risk,
        "verdict": release_info.get("verdict"),
        "current_version": release_info.get("current_version"),
        "artifact_version": release_info.get("artifact_version"),
        "artifact_version_delta": release_info.get("artifact_version_delta"),
        "failure_code": release_info.get("failure_code"),
        "evidence_root": release_info.get("evidence_root"),
    }


def _live_proof_summary(live_proof_info: Mapping[str, Any]) -> dict[str, Any]:
    status = str(live_proof_info.get("status") or "not_configured")
    return {
        "status": status,
        "applies": bool(live_proof_info.get("applies")),
        "version_match": live_proof_info.get("version_match"),
        "current_version": live_proof_info.get("current_version"),
        "artifact_version": live_proof_info.get("artifact_version"),
        "verdict": live_proof_info.get("verdict"),
        "failure_code": live_proof_info.get("failure_code"),
        "freshness_status": live_proof_info.get("freshness_status"),
        "evidence_root": live_proof_info.get("evidence_root"),
    }


def _provider_state(
    *,
    cli_info: Mapping[str, Any],
    contract_requires_cli: bool,
    version_readiness: Mapping[str, Any],
    proof: Mapping[str, Any],
    control_connected: bool,
    expected_supports: tuple[str, ...],
    live_control_operations: tuple[str, ...],
    missing_live_control_operations: tuple[str, ...],
) -> str:
    if contract_requires_cli and _cli_state(cli_info) != "available":
        return "provider_cli_missing"
    if version_readiness.get("risk") == "blocking":
        return "blocked"
    if proof.get("release_failed_operations"):
        return "needs_attention"
    if proof.get("local_proof_failed_operations"):
        return "needs_attention"
    if proof.get("local_proof_verdict_failed"):
        return "needs_attention"
    if expected_supports and not control_connected:
        return "live_control_not_connected"
    if expected_supports and not live_control_operations:
        return "live_control_partial"
    if expected_supports and missing_live_control_operations:
        return "live_control_partial"
    return "ready"


def _live_control_state(
    *,
    control_connected: bool,
    expected_supports: tuple[str, ...],
    live_control_operations: tuple[str, ...],
    missing_live_control_operations: tuple[str, ...],
) -> str:
    if not expected_supports:
        return "not_applicable"
    if not control_connected:
        return "not_connected"
    if not live_control_operations:
        return "not_advertised"
    if missing_live_control_operations:
        return "partial"
    return "advertised"


def _summary(providers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_state: dict[str, list[str]] = {}
    for provider, info in providers.items():
        state = str(info.get("state") or "unknown")
        by_state.setdefault(state, []).append(provider)
    return {
        "providers_count": len(providers),
        "by_state": {state: sorted(items) for state, items in sorted(by_state.items())},
    }
