"""Read-only comparison between served and reducer-backed session state."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict

from zerg.services.session_state_contract import SessionActivityFacts
from zerg.services.session_state_contract import SessionControlFacts
from zerg.services.session_state_contract import SessionStateFacts
from zerg.services.session_state_facts_projector import ShadowSessionStateProjection


class SessionStateAxisComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    matches: bool
    legacy: dict[str, Any] | None
    shadow: dict[str, Any] | None


class SessionControlIdentityComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal[
        "legacy_only",
        "unbound",
        "bound_matched",
        "identity_diverged",
        "binding_unknown",
    ]
    evidence: dict[str, str | None] | None = None
    catalog: dict[str, str | None] | None = None
    legacy_grant: dict[str, str | int | None] | None = None
    catalog_bound_count: int = 0


class SessionStateDeltaClassification(BaseModel):
    model_config = ConfigDict(frozen=True)

    family: Literal["mode", "disposition", "launch", "run", "activity", "control", "control_identity"]
    legacy_source: Literal["legacy_runtime", "legacy_semantic", "legacy_capability", "none"]
    canonical_source: Literal["durable_catalog", "activity_head", "control_head", "catalog_binding", "none"]
    relation: Literal[
        "same_coordinate",
        "expired",
        "historical_only",
        "missing_typed_evidence",
        "rejected_typed_evidence",
        "identity_mismatch",
        "semantic_divergence",
    ]
    resolution: Literal["accept_canonical", "require_targeted_proof", "block_deletion"]
    reason: str


class SessionStateComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["matched", "different", "not_comparable"]
    same_commit: bool
    mode: SessionStateAxisComparison | None = None
    disposition: SessionStateAxisComparison | None = None
    launch: SessionStateAxisComparison | None = None
    run: SessionStateAxisComparison | None = None
    activity: SessionStateAxisComparison | None = None
    control: SessionStateAxisComparison | None = None
    control_identity: SessionControlIdentityComparison | None = None
    deltas: tuple[SessionStateDeltaClassification, ...] = ()
    gate_status: Literal["clear", "targeted_proof_required", "blocked"] = "clear"


class SessionStateProjectionSignature(BaseModel):
    model_config = ConfigDict(frozen=True)

    commit_seq: int | None
    state_contract_version: int
    presentation_policy_version: int
    primary_key: str | None
    access_key: str | None


class SessionStateProjectionParity(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["matched", "diverged", "not_comparable"]
    canonical_facts: SessionStateProjectionSignature
    compact_projection: SessionStateProjectionSignature
    mismatched_fields: tuple[str, ...] = ()


def compare_session_state_projections(
    *,
    session_state: SessionStateFacts,
    machine_payload: dict[str, Any],
) -> SessionStateProjectionParity:
    """Check compact serialization against facts from one catalog snapshot."""

    canonical = SessionStateProjectionSignature(
        commit_seq=session_state.commit_seq,
        state_contract_version=session_state.state_contract_version,
        presentation_policy_version=session_state.presentation_policy_version,
        primary_key=session_state.presentation.primary.key if session_state.presentation.primary else None,
        access_key=session_state.presentation.access.key if session_state.presentation.access else None,
    )
    presentation = machine_payload.get("presentation")
    if not isinstance(presentation, dict):
        presentation = {}
    primary = presentation.get("primary")
    access = presentation.get("access")
    compact = SessionStateProjectionSignature(
        commit_seq=_optional_int(machine_payload.get("commit_seq")),
        state_contract_version=_required_int(machine_payload.get("state_contract_version"), "state_contract_version"),
        presentation_policy_version=_required_int(
            machine_payload.get("presentation_policy_version"),
            "presentation_policy_version",
        ),
        primary_key=primary.get("key") if isinstance(primary, dict) else None,
        access_key=access.get("key") if isinstance(access, dict) else None,
    )
    if canonical.commit_seq is None or compact.commit_seq is None or canonical.commit_seq != compact.commit_seq:
        return SessionStateProjectionParity(
            status="not_comparable",
            canonical_facts=canonical,
            compact_projection=compact,
        )
    mismatched = tuple(
        field
        for field in ("state_contract_version", "presentation_policy_version", "primary_key", "access_key")
        if getattr(canonical, field) != getattr(compact, field)
    )
    return SessionStateProjectionParity(
        status="diverged" if mismatched else "matched",
        canonical_facts=canonical,
        compact_projection=compact,
        mismatched_fields=mismatched,
    )


def _required_int(value: Any, field: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise ValueError(f"compact projection is missing {field}")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer contract coordinate")
    return int(value)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def compare_session_state_axes(
    *,
    legacy: SessionStateFacts,
    shadow: ShadowSessionStateProjection,
    legacy_commit_seq: int,
    shadow_commit_seq: int,
    catalog_facts: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> SessionStateComparison:
    """Compare only axes the non-served projector can derive independently."""

    if legacy_commit_seq != shadow_commit_seq:
        return SessionStateComparison(status="not_comparable", same_commit=False)

    mode = _axis({"state": legacy.mode}, {"state": shadow.mode})
    disposition = _axis(_payload(legacy.disposition), _payload(shadow.disposition))
    launch = _axis(_payload(legacy.launch), _payload(shadow.launch))
    run = _axis(_payload(legacy.run), _payload(shadow.run))
    activity = _axis(_activity_payload(legacy.activity), _activity_payload(shadow.activity))
    control = _axis(_control_payload(legacy.control), _control_payload(shadow.control))
    control_identity = _control_identity(catalog_facts, shadow)
    comparisons = (mode, disposition, launch, run, activity, control)
    deltas = _classify_deltas(
        legacy=legacy,
        shadow=shadow,
        comparisons={
            "mode": mode,
            "disposition": disposition,
            "launch": launch,
            "run": run,
            "activity": activity,
            "control": control,
        },
        control_identity=control_identity,
        now=_aware(now or datetime.now(UTC)),
    )
    gate_status: Literal["clear", "targeted_proof_required", "blocked"]
    if any(delta.resolution == "block_deletion" for delta in deltas):
        gate_status = "blocked"
    elif any(delta.resolution == "require_targeted_proof" for delta in deltas):
        gate_status = "targeted_proof_required"
    else:
        gate_status = "clear"
    return SessionStateComparison(
        status="matched" if all(comparison.matches for comparison in comparisons) else "different",
        same_commit=True,
        mode=mode,
        disposition=disposition,
        launch=launch,
        run=run,
        activity=activity,
        control=control,
        control_identity=control_identity,
        deltas=deltas,
        gate_status=gate_status,
    )


def _classify_deltas(
    *,
    legacy: SessionStateFacts,
    shadow: ShadowSessionStateProjection,
    comparisons: dict[str, SessionStateAxisComparison],
    control_identity: SessionControlIdentityComparison | None,
    now: datetime,
) -> tuple[SessionStateDeltaClassification, ...]:
    deltas: list[SessionStateDeltaClassification] = []
    for family, comparison in comparisons.items():
        if comparison.matches:
            continue
        if family == "activity":
            deltas.append(_classify_activity_delta(legacy.activity, shadow, now=now))
        elif family == "control":
            deltas.append(_classify_control_delta(legacy.control, shadow, control_identity))
        elif family == "run":
            deltas.append(_classify_run_delta(legacy, shadow))
        else:
            deltas.append(
                SessionStateDeltaClassification(
                    family=family,
                    legacy_source="legacy_runtime",
                    canonical_source="durable_catalog",
                    relation="semantic_divergence",
                    resolution="block_deletion",
                    reason=f"unclassified {family} divergence",
                )
            )
    return tuple(deltas)


def _classify_activity_delta(
    legacy: SessionActivityFacts,
    shadow: ShadowSessionStateProjection,
    *,
    now: datetime,
) -> SessionStateDeltaClassification:
    canonical = shadow.activity
    legacy_payload = legacy.model_dump(mode="json")
    canonical_payload = canonical.model_dump(mode="json")
    legacy_without_expiry = {key: value for key, value in legacy_payload.items() if key != "valid_until"}
    canonical_without_expiry = {key: value for key, value in canonical_payload.items() if key != "valid_until"}
    if legacy_without_expiry == canonical_without_expiry:
        return SessionStateDeltaClassification(
            family="activity",
            legacy_source="legacy_semantic",
            canonical_source="activity_head",
            relation="same_coordinate",
            resolution="accept_canonical",
            reason="typed activity validity replaces the legacy freshness window",
        )
    if (
        canonical.state == "unknown"
        and canonical.observed_at is None
        and canonical.valid_until is None
        and canonical.source is None
        and canonical.tool is None
        and legacy.state == "unknown"
        and legacy.observed_at is not None
        and legacy.valid_until is not None
        and legacy.valid_until <= now
        and shadow.rejected_activity_heads == 0
    ):
        return SessionStateDeltaClassification(
            family="activity",
            legacy_source="legacy_semantic",
            canonical_source="none",
            relation="expired",
            resolution="accept_canonical",
            reason="expired legacy activity metadata is not current evidence",
        )
    return SessionStateDeltaClassification(
        family="activity",
        legacy_source="legacy_semantic",
        canonical_source="activity_head" if "activity" in shadow.fact_sources else "none",
        relation="rejected_typed_evidence" if shadow.rejected_activity_heads else "semantic_divergence",
        resolution="block_deletion",
        reason=(
            "typed activity evidence was rejected by durable run binding"
            if shadow.rejected_activity_heads
            else "activity semantics differ beyond expiry policy"
        ),
    )


def _classify_control_delta(
    legacy: SessionControlFacts,
    shadow: ShadowSessionStateProjection,
    identity: SessionControlIdentityComparison | None,
) -> SessionStateDeltaClassification:
    canonical = shadow.control
    if shadow.rejected_control_heads:
        return SessionStateDeltaClassification(
            family="control",
            legacy_source="legacy_capability",
            canonical_source="control_head",
            relation="rejected_typed_evidence",
            resolution="block_deletion",
            reason="typed control evidence is not bound to the durable run and connection",
        )
    if canonical is None:
        return SessionStateDeltaClassification(
            family="control",
            legacy_source="legacy_capability",
            canonical_source="none",
            relation="missing_typed_evidence",
            resolution="require_targeted_proof",
            reason="historical control has no current typed head",
        )
    if identity is None or identity.status != "bound_matched":
        return SessionStateDeltaClassification(
            family="control",
            legacy_source="legacy_capability",
            canonical_source="catalog_binding",
            relation="identity_mismatch",
            resolution="block_deletion",
            reason="current control identity is not bound to the durable catalog connection",
        )
    legacy_actions = legacy.actions.model_dump(mode="json")
    canonical_actions = canonical.actions.model_dump(mode="json")
    broadened = [
        name
        for name, action in canonical_actions.items()
        if action.get("state") == "available" and legacy_actions.get(name, {}).get("state") != "available"
    ]
    same_identity = (
        str(legacy.connection_id or "") == str(canonical.connection_id or "")
        and legacy.lease_generation == canonical.lease_generation
        and legacy.connection == canonical.connection
    )
    if same_identity and not broadened:
        return SessionStateDeltaClassification(
            family="control",
            legacy_source="legacy_capability",
            canonical_source="control_head",
            relation="same_coordinate",
            resolution="accept_canonical",
            reason="exact typed grants safely narrow legacy capabilities",
        )
    return SessionStateDeltaClassification(
        family="control",
        legacy_source="legacy_capability",
        canonical_source="control_head",
        relation="semantic_divergence",
        resolution="block_deletion",
        reason="control identity changed or canonical grants broadened legacy authority",
    )


def _classify_run_delta(
    legacy: SessionStateFacts,
    shadow: ShadowSessionStateProjection,
) -> SessionStateDeltaClassification:
    legacy_run = legacy.run
    canonical_run = shadow.run
    if (
        legacy_run is not None
        and canonical_run is not None
        and legacy_run.id == canonical_run.id
        and legacy_run.lifecycle == "ended"
        and legacy_run.ended_at is None
        and canonical_run.lifecycle == "running"
        and canonical_run.ended_at is None
    ):
        return SessionStateDeltaClassification(
            family="run",
            legacy_source="legacy_runtime",
            canonical_source="durable_catalog",
            relation="historical_only",
            resolution="accept_canonical",
            reason="legacy terminal state without durable ended_at is not terminal provenance",
        )
    if (
        legacy_run is not None
        and canonical_run is not None
        and legacy_run.id == canonical_run.id
        and legacy_run.lifecycle == "running"
        and legacy_run.ended_at is None
        and canonical_run.lifecycle == "ended"
        and canonical_run.ended_at is not None
    ):
        return SessionStateDeltaClassification(
            family="run",
            legacy_source="legacy_runtime",
            canonical_source="durable_catalog",
            relation="historical_only",
            resolution="accept_canonical",
            reason="durable catalog terminal provenance supersedes stale legacy running state",
        )
    return SessionStateDeltaClassification(
        family="run",
        legacy_source="legacy_runtime",
        canonical_source="durable_catalog",
        relation="semantic_divergence",
        resolution="block_deletion",
        reason="run lifecycle differs beyond the historical non-durable terminal correction",
    )


def _axis(legacy: dict[str, Any] | None, shadow: dict[str, Any] | None) -> SessionStateAxisComparison:
    return SessionStateAxisComparison(matches=legacy == shadow, legacy=legacy, shadow=shadow)


def _payload(value: BaseModel | None) -> dict[str, Any] | None:
    return value.model_dump(mode="json") if value is not None else None


def _activity_payload(activity: SessionActivityFacts) -> dict[str, Any]:
    return activity.model_dump(mode="json")


def _control_payload(control: SessionControlFacts | None) -> dict[str, Any] | None:
    if control is None:
        return None
    payload = control.model_dump(mode="json")
    connection_id = payload.get("connection_id")
    if connection_id is not None:
        payload["connection_id"] = str(connection_id)
    actions = payload.get("actions")
    if isinstance(actions, dict):
        payload["actions"] = {name: actions[name] for name in sorted(actions)}
    return payload


def _control_identity(
    catalog_facts: dict[str, Any] | None,
    shadow: ShadowSessionStateProjection,
) -> SessionControlIdentityComparison | None:
    if not isinstance(catalog_facts, dict):
        return None
    connections = catalog_facts.get("connections")
    if not isinstance(connections, list):
        connections = []
    evidence = None
    if shadow.control is not None:
        evidence = {
            "run_id": shadow.control_run_id,
            "adapter_connection_id": str(shadow.control.connection_id or "") or None,
            "lease_generation": shadow.control.lease_generation,
        }
    if evidence is None:
        return SessionControlIdentityComparison(status="legacy_only")

    bound_connections = [row for row in connections if isinstance(row, dict) and row.get("adapter_connection_id") is not None]
    bound = next(
        (
            row
            for row in connections
            if isinstance(row, dict)
            and row.get("adapter_connection_id") is not None
            and str(row.get("adapter_connection_id")) == evidence["adapter_connection_id"]
        ),
        None,
    )
    if bound is None:
        if bound_connections:
            return SessionControlIdentityComparison(
                status="binding_unknown",
                evidence=evidence,
                catalog_bound_count=len(bound_connections),
            )
        return SessionControlIdentityComparison(
            status="unbound",
            evidence=evidence,
        )

    catalog = _catalog_identity(bound)
    matches = (
        catalog["run_id"] == evidence["run_id"]
        and catalog["adapter_connection_id"] == evidence["adapter_connection_id"]
        and catalog["lease_generation"] == evidence["lease_generation"]
    )
    return SessionControlIdentityComparison(
        status="bound_matched" if matches else "identity_diverged",
        evidence=evidence,
        catalog=catalog,
        legacy_grant=_legacy_grant_identity(bound),
        catalog_bound_count=len(bound_connections),
    )


def _catalog_identity(connection: dict[str, Any] | None) -> dict[str, str | None] | None:
    if connection is None:
        return None
    return {
        "run_id": str(connection.get("run_id") or "") or None,
        "adapter_connection_id": str(connection.get("adapter_connection_id") or "") or None,
        "lease_generation": str(connection.get("lease_generation") or "") or None,
    }


def _legacy_grant_identity(connection: dict[str, Any] | None) -> dict[str, str | int | None] | None:
    if connection is None:
        return None
    connection_id = connection.get("id")
    adapter_connection_id = str(connection.get("adapter_connection_id") or "") or None
    adapter_generation = str(connection.get("lease_generation") or "") or None
    acquired_at = str(connection.get("acquired_at") or "") or None
    if adapter_connection_id and adapter_generation:
        return {
            "catalog_connection_id": connection_id if isinstance(connection_id, int) else None,
            "connection_id": adapter_connection_id,
            "lease_generation": adapter_generation,
            "identity_source": "adapter_bound",
        }
    return {
        "catalog_connection_id": connection_id if isinstance(connection_id, int) else None,
        "connection_id": connection_id if isinstance(connection_id, int) else None,
        "lease_generation": f"{connection_id}:{acquired_at}" if connection_id is not None and acquired_at else None,
        "identity_source": "legacy_synthetic",
    }


__all__ = [
    "SessionStateAxisComparison",
    "SessionStateComparison",
    "SessionControlIdentityComparison",
    "SessionStateDeltaClassification",
    "SessionStateProjectionParity",
    "SessionStateProjectionSignature",
    "compare_session_state_axes",
    "compare_session_state_projections",
]
