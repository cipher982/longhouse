from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zerg.services.managed_provider_contracts import all_managed_provider_contracts

from ._shared import _normalize_optional_string
from ._shared import _with_action


def _apply_managed_session_contract_diagnostics(
    *,
    diagnostics: Mapping[str, Any],
    reasons: list[str],
    suggested_actions: list[str],
    managed_sessions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw_issues = diagnostics.get("issues")
    if not isinstance(raw_issues, list):
        return None
    issues = [issue for issue in raw_issues if isinstance(issue, Mapping)]
    if not issues:
        return None

    issue_reasons_by_session: dict[str, list[str]] = {}
    for issue in issues:
        reason = _normalize_optional_string(issue.get("reason"))
        if reason and reason not in reasons:
            reasons.append(reason)
        action = _normalize_optional_string(issue.get("action"))
        if action:
            _with_action(suggested_actions, action)
        session_id = _normalize_optional_string(issue.get("session_id"))
        if session_id and reason:
            issue_reasons_by_session.setdefault(session_id, []).append(reason)

    for session in managed_sessions:
        session_id = _normalize_optional_string(session.get("session_id"))
        if not session_id or session_id not in issue_reasons_by_session:
            continue
        reason_codes = list(session.get("reason_codes") or [])
        for reason in issue_reasons_by_session[session_id]:
            if reason not in reason_codes:
                reason_codes.append(reason)
        session["reason_codes"] = reason_codes
        if session.get("state") == "attached":
            session["state"] = "degraded"

    return dict(issues[0])


def _managed_contract_headline(diagnostics: Mapping[str, Any], latest_issue: Mapping[str, Any]) -> str:
    raw_issues = diagnostics.get("issues")
    issues = [issue for issue in raw_issues if isinstance(issue, Mapping)] if isinstance(raw_issues, list) else []
    session_ids = set()
    for issue in issues:
        session_id = _normalize_optional_string(issue.get("session_id"))
        if session_id is not None:
            session_ids.add(session_id)
    if len(session_ids) > 1:
        return f"{len(session_ids)} managed provider sessions need attention"
    if len(issues) > 1:
        return f"{len(issues)} managed provider session issues need attention"
    return _normalize_optional_string(latest_issue.get("headline")) or "Managed provider session needs attention"


def _collect_provider_contracts() -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for contract in all_managed_provider_contracts():
        operations: dict[str, Any] = {}
        for operation, evidence in sorted(contract.operation_evidence.items()):
            supported = bool(getattr(contract, operation, False))
            operations[operation] = {
                "supported": supported,
                "evidence_level": evidence.get("level"),
                "evidence_source": evidence.get("source"),
                "next": evidence.get("next"),
            }
        providers[contract.provider] = {
            "managed_transport": contract.managed_transport.value,
            "control_plane": contract.control_plane,
            "control_plane_aliases": list(contract.control_plane_aliases),
            "machine_control_supports": list(contract.machine_control_supports),
            "operations": operations,
        }
    return {
        "schema_version": 1,
        "providers": providers,
    }


__all__ = ["_apply_managed_session_contract_diagnostics", "_managed_contract_headline", "_collect_provider_contracts"]
