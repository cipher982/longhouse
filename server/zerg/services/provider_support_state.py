"""Managed provider support-state projection.

This read model joins three separate facts without collapsing them:

- contract capability: what Longhouse implements for a provider
- proof maturity: how strongly each supported operation is verified
- version readiness: whether release-drift evidence applies to the installed CLI

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


def collect_provider_support_state(
    *,
    provider_clis: Mapping[str, Any],
    provider_release_status: Mapping[str, Any],
    control_channel: Mapping[str, Any],
) -> dict[str, Any]:
    release_statuses = dict(provider_release_status.get("statuses") or {})
    live_ops_by_provider = dict(control_channel.get("control_operations_by_provider") or {})
    raw_control_status = str(control_channel.get("status") or "").strip()
    control_connected = raw_control_status == "connected"

    providers: dict[str, Any] = {}
    for contract in all_managed_provider_contracts():
        provider = contract.provider
        cli_info = dict(provider_clis.get(provider) or {})
        release_info = dict(release_statuses.get(provider) or {})
        live_control_operations = tuple(str(item) for item in live_ops_by_provider.get(provider) or ())
        operations = _operation_states(contract)
        version_readiness = _version_readiness(release_info)
        provider_state = _provider_state(
            cli_info=cli_info,
            contract_requires_cli=bool(getattr(contract, "launch_local", False)),
            version_readiness=version_readiness,
            control_connected=control_connected,
            expected_supports=contract.machine_control_supports,
            live_control_operations=live_control_operations,
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
                "live_control_state": _live_control_state(
                    control_connected=control_connected,
                    expected_supports=contract.machine_control_supports,
                    live_control_operations=live_control_operations,
                ),
            },
            "proof": _proof_summary(operations),
            "operations": operations,
            "version_readiness": version_readiness,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "summary": _summary(providers),
        "providers": providers,
    }


def _operation_states(contract: Any) -> dict[str, dict[str, Any]]:
    operations: dict[str, dict[str, Any]] = {}
    for operation in CONTRACT_OPERATIONS:
        supported = bool(getattr(contract, operation, False))
        evidence = dict(contract.operation_evidence_for(operation))
        level = str(evidence.get("level") or "none")
        operations[operation] = {
            "supported": supported,
            "evidence_level": level,
            "evidence_rank": EVIDENCE_RANK.get(level, -1),
            "evidence_source": evidence.get("source"),
            "next": evidence.get("next"),
        }
    return operations


def _operation_names_by_support(operations: Mapping[str, Mapping[str, Any]], *, supported: bool) -> list[str]:
    return [operation for operation, info in operations.items() if bool(info.get("supported")) == supported]


def _proof_summary(operations: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
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
    state = "scheduled_live_token" if minimum_rank >= EVIDENCE_RANK["scheduled_live_token"] else "mixed"
    return {
        "state": state,
        "minimum_evidence_level": minimum_level,
        "minimum_evidence_operations": minimum_operations,
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


def _provider_state(
    *,
    cli_info: Mapping[str, Any],
    contract_requires_cli: bool,
    version_readiness: Mapping[str, Any],
    control_connected: bool,
    expected_supports: tuple[str, ...],
    live_control_operations: tuple[str, ...],
) -> str:
    if contract_requires_cli and _cli_state(cli_info) != "available":
        return "provider_cli_missing"
    if version_readiness.get("risk") == "blocking":
        return "blocked"
    if version_readiness.get("risk") == "warning":
        return "needs_attention"
    if expected_supports and not control_connected:
        return "live_control_not_connected"
    if expected_supports and not live_control_operations:
        return "live_control_partial"
    return "ready"


def _live_control_state(
    *,
    control_connected: bool,
    expected_supports: tuple[str, ...],
    live_control_operations: tuple[str, ...],
) -> str:
    if not expected_supports:
        return "not_applicable"
    if not control_connected:
        return "not_connected"
    if not live_control_operations:
        return "not_advertised"
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
