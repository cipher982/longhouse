"""Read-only comparison between served and reducer-backed session state."""

from __future__ import annotations

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


def compare_session_state_axes(
    *,
    legacy: SessionStateFacts,
    shadow: ShadowSessionStateProjection,
    legacy_commit_seq: int,
    shadow_commit_seq: int,
    catalog_facts: dict[str, Any] | None = None,
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
        payload["actions"] = {name: actions.get(name) for name in ("send_input", "interrupt", "terminate", "reattach", "resume")}
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
    acquired_at = str(connection.get("acquired_at") or "") or None
    return {
        "catalog_connection_id": connection_id if isinstance(connection_id, int) else None,
        # The served control path synthesizes this token from catalog row identity.
        # It is intentionally not comparable to the adapter's lease_generation.
        "synthetic_generation": f"{connection_id}:{acquired_at}" if connection_id is not None and acquired_at else None,
    }


__all__ = [
    "SessionStateAxisComparison",
    "SessionStateComparison",
    "SessionControlIdentityComparison",
    "compare_session_state_axes",
]
