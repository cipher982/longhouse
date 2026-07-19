"""Pure, non-served session-state projection from one catalog snapshot.

Reducer heads provide expiring machine observations. Durable catalog rows in
the same snapshot provide lifecycle facts. The projection remains diagnostic
until every served and authorized path is cut over explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from zerg.machine_evidence import canonical_evidence_hash
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_state_contract import STATE_CONTRACT_VERSION
from zerg.services.session_state_contract import SessionActionAvailability
from zerg.services.session_state_contract import SessionActivityFacts
from zerg.services.session_state_contract import SessionControlActions
from zerg.services.session_state_contract import SessionControlFacts
from zerg.services.session_state_contract import SessionDispositionFacts
from zerg.services.session_state_contract import SessionHostFacts
from zerg.services.session_state_contract import SessionLaunchFacts
from zerg.services.session_state_contract import SessionMode
from zerg.services.session_state_contract import SessionPendingInteractionFacts
from zerg.services.session_state_contract import SessionRunFacts
from zerg.services.session_state_contract import SessionStateFacts
from zerg.services.session_state_contract import SessionTranscriptFacts
from zerg.services.session_state_contract import assemble_session_state_facts

UnsupportedFactFamily = Literal[
    "mode",
    "disposition",
    "launch",
    "run",
    "pending_interaction",
    "transcript",
    "host",
    "presentation",
]

SHADOW_SUPPORTED_FAMILIES: tuple[str, ...] = ("mode", "disposition", "launch", "run", "activity", "control")
SHADOW_UNSUPPORTED_FAMILIES: tuple[UnsupportedFactFamily, ...] = (
    "pending_interaction",
    "transcript",
    "host",
    "presentation",
)
_AUTHORITY_RANK = {
    ("activity", "provider_runtime"): 1,
    ("control", "provider_control"): 1,
}
_ACTIVITY_STATE = {
    "thinking": "thinking",
    "running": "executing",
    "idle": "quiescent",
    "needs_user": "quiescent",
    "blocked": "blocked",
    "stalled": "stalled",
}
_ACTION_OPERATION = {
    "send_input": "send_input",
    "interrupt": "interrupt",
    "terminate": "terminate",
    "resume": "resume",
}
_GRANTED_OPERATIONS = frozenset({"send_input", "interrupt", "terminate", "tail_output", "resume"})


class ShadowSessionStateProjection(BaseModel):
    """Non-served projection with unsupported axes named explicitly."""

    model_config = ConfigDict(frozen=True)

    state_contract_version: int = STATE_CONTRACT_VERSION
    commit_seq: int
    mode: SessionMode
    disposition: SessionDispositionFacts
    launch: SessionLaunchFacts | None = None
    run: SessionRunFacts | None = None
    activity: SessionActivityFacts
    control: SessionControlFacts | None
    control_run_id: str | None = None
    fact_sources: dict[str, "FactHeadDiagnostic"] = Field(default_factory=dict)
    rejected_heads: int = 0
    unsupported_families: tuple[UnsupportedFactFamily, ...] = SHADOW_UNSUPPORTED_FAMILIES


class FactHeadDiagnostic(BaseModel):
    """Winning reducer coordinate used by the pure projection."""

    model_config = ConfigDict(frozen=True)

    family: Literal["activity", "control"]
    subject_key: str
    source: str
    source_epoch: str
    evidence_hash: str
    observed_at: datetime
    valid_until: datetime
    updated_commit_seq: int | None = None


class ControlFactAuthorization(BaseModel):
    """Fail-closed authorization result for one exact reducer subject."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str | None = None
    control: SessionControlFacts | None = None
    run_id: str | None = None


def project_shadow_session_state_facts(
    *,
    session_id: str,
    commit_seq: int,
    catalog_facts: Mapping[str, Any],
    heads: Collection[Mapping[str, Any]],
    supported_operations: Collection[str] = (),
    now: datetime,
) -> ShadowSessionStateProjection:
    """Project durable and observed axes from one coherent catalog snapshot."""

    normalized_now = _aware(now, "now")
    activity_head, rejected_activity = _effective_head(
        heads,
        session_id=session_id,
        family="activity",
        now=normalized_now,
    )
    control_head, rejected_control = _effective_head(
        heads,
        session_id=session_id,
        family="control",
        now=normalized_now,
    )
    fact_sources = {
        family: diagnostic
        for family, winner in (("activity", activity_head), ("control", control_head))
        if (diagnostic := _head_diagnostic(family, winner)) is not None
    }
    launch = _project_launch(catalog_facts)
    return ShadowSessionStateProjection(
        commit_seq=commit_seq,
        mode=_project_mode(catalog_facts),
        disposition=_project_disposition(catalog_facts),
        launch=launch,
        run=_project_run(catalog_facts, launch=launch),
        activity=_project_activity(activity_head),
        control=_project_control(control_head, supported_operations=set(supported_operations)),
        control_run_id=_control_run_id(control_head),
        fact_sources=fact_sources,
        rejected_heads=rejected_activity + rejected_control,
    )


def project_served_session_state_facts(
    *,
    session_id: str,
    commit_seq: int,
    catalog_facts: Mapping[str, Any],
    heads: Collection[Mapping[str, Any]],
    supported_operations: Collection[str],
    catalog_capabilities: KernelSessionCapabilities,
    pending_interaction: SessionPendingInteractionFacts | None,
    transcript: SessionTranscriptFacts,
    host: SessionHostFacts,
    now: datetime,
) -> SessionStateFacts:
    """Project the full served contract from one bounded catalog snapshot."""

    shadow = project_shadow_session_state_facts(
        session_id=session_id,
        commit_seq=commit_seq,
        catalog_facts=catalog_facts,
        heads=heads,
        supported_operations=supported_operations,
        now=now,
    )
    control = _served_control(
        shadow.control,
        mode=shadow.mode,
        capabilities=catalog_capabilities,
    )
    return assemble_session_state_facts(
        mode=shadow.mode,
        disposition=shadow.disposition,
        launch=shadow.launch,
        run=shadow.run,
        activity=shadow.activity,
        control=control,
        pending_interaction=pending_interaction,
        transcript=transcript,
        host=host,
        commit_seq=commit_seq,
    )


def authorize_exact_control_fact(
    *,
    session_id: str,
    run_id: str,
    provider: str,
    connection_id: str,
    lease_generation: str,
    operation: Literal["send_input", "interrupt", "terminate"],
    heads: Collection[Mapping[str, Any]],
    supported_operations: Collection[str],
    now: datetime,
) -> ControlFactAuthorization:
    """Authorize from the exact bound control subject, never a session-wide winner."""

    expected_subject = f"connection:{connection_id}:{lease_generation}"
    exact_heads = [head for head in heads if head.get("family") == "control" and head.get("subject_key") == expected_subject]
    if not exact_heads:
        return ControlFactAuthorization(allowed=False, reason="control_head_missing")
    winner, rejected = _effective_head(
        exact_heads,
        session_id=session_id,
        family="control",
        now=_aware(now, "now"),
    )
    if rejected:
        return ControlFactAuthorization(allowed=False, reason="control_head_rejected")
    if winner is None:
        return ControlFactAuthorization(allowed=False, reason="lease_expired")
    _head, value, _observed_at, _valid_until = winner
    if (
        _text(value.get("provider")) != provider
        or _text(value.get("run_id")) != run_id
        or _text(value.get("connection_id")) != connection_id
        or _text(value.get("lease_generation")) != lease_generation
    ):
        return ControlFactAuthorization(allowed=False, reason="identity_diverged")
    if operation not in supported_operations:
        return ControlFactAuthorization(allowed=False, reason="unsupported")
    control = _project_control(winner, supported_operations=set(supported_operations))
    if control is None:
        return ControlFactAuthorization(allowed=False, reason="control_head_missing")
    action = getattr(control.actions, operation)
    if action.state != "available":
        return ControlFactAuthorization(
            allowed=False,
            reason=action.reason or "control_unavailable",
            control=control,
            run_id=_control_run_id(winner),
        )
    return ControlFactAuthorization(
        allowed=True,
        control=control,
        run_id=_control_run_id(winner),
    )


def _served_control(
    observed: SessionControlFacts | None,
    *,
    mode: SessionMode,
    capabilities: KernelSessionCapabilities,
) -> SessionControlFacts:
    def availability(available: bool, reason: str) -> SessionActionAvailability:
        if available:
            return SessionActionAvailability(state="available")
        return SessionActionAvailability(state="unavailable", reason=reason)

    start_turn = availability(
        mode == "console" and capabilities.can_start_turn,
        capabilities.start_turn_blocked_by or ("not_console" if mode != "console" else "start_turn_unavailable"),
    )
    if observed is not None:
        reattach = availability(
            observed.connection != "connected" and capabilities.host_reattach_available,
            "already_connected" if observed.connection == "connected" else "reattach_unavailable",
        )
        return observed.model_copy(
            update={
                "control_plane": capabilities.control_plane,
                "actions": observed.actions.model_copy(
                    update={"start_turn": start_turn, "reattach": reattach},
                ),
            }
        )

    owned = mode in {"helm", "console"} or capabilities.control_owned or capabilities.host_reattach_available
    connection = "not_applicable" if mode in {"shadow", "console"} else "unknown"
    unavailable_reason = "observe_only" if not owned else "control_unknown"
    reattach = availability(
        mode == "helm" and capabilities.host_reattach_available,
        "reattach_unavailable",
    )
    resume = availability(
        mode == "helm" and capabilities.host_reattach_available and capabilities.can_resume,
        "resume_unavailable",
    )
    return SessionControlFacts(
        ownership="owned" if owned else "unowned",
        connection=connection,
        control_plane=capabilities.control_plane,
        actions=SessionControlActions(
            start_turn=start_turn,
            send_input=availability(False, unavailable_reason),
            interrupt=availability(False, unavailable_reason),
            terminate=availability(False, unavailable_reason),
            reattach=reattach,
            resume=resume,
        ),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _project_mode(catalog_facts: Mapping[str, Any]) -> SessionMode:
    catalog = _mapping(catalog_facts.get("catalog"))
    readiness = _mapping(catalog_facts.get("readiness"))
    run = _mapping(catalog_facts.get("latest_run"))
    origin_kind = _text(catalog.get("origin_kind"))
    launch_surface = _text(catalog.get("launch_surface"))
    execution_lifetime = _text(readiness.get("execution_lifetime"))
    launch_origin = _text(run.get("launch_origin"))
    connections = catalog_facts.get("connections")

    if origin_kind == "console" or execution_lifetime == "one_shot" or launch_surface in {"web", "ios", "api"}:
        return "console"
    if (
        execution_lifetime == "live_control"
        or (isinstance(connections, list) and bool(connections))
        or launch_origin in {"longhouse_spawned", "longhouse_continued"}
    ):
        return "helm"
    return "shadow"


def _project_disposition(catalog_facts: Mapping[str, Any]) -> SessionDispositionFacts:
    catalog = _mapping(catalog_facts.get("catalog"))
    closed_at = _optional_wire_datetime(catalog.get("closed_at"), "catalog.closed_at")
    if closed_at is None:
        return SessionDispositionFacts(state="open")
    return SessionDispositionFacts(
        state="closed",
        closed_at=closed_at,
        close_reason=_text(catalog.get("close_reason")) or "user_closed",
    )


def _project_launch(catalog_facts: Mapping[str, Any]) -> SessionLaunchFacts | None:
    readiness = _mapping(catalog_facts.get("readiness"))
    raw_state = _text(readiness.get("state"))
    state = {
        "pending": "pending",
        "dispatched": "dispatched",
        "failed": "failed",
        "adopted": "adopted",
        "abandoned": "abandoned",
    }.get(raw_state)
    if state is None:
        return None
    return SessionLaunchFacts(
        state=state,
        error_code=_text(readiness.get("error_code")),
        error_message=_text(readiness.get("error_message")),
    )


def _project_run(
    catalog_facts: Mapping[str, Any],
    *,
    launch: SessionLaunchFacts | None,
) -> SessionRunFacts | None:
    run = _mapping(catalog_facts.get("latest_run"))
    run_id = _text(run.get("id"))
    if run_id is None:
        if launch is not None and launch.state in {"pending", "dispatched"}:
            catalog = _mapping(catalog_facts.get("catalog"))
            return SessionRunFacts(
                lifecycle="starting",
                started_at=_optional_wire_datetime(catalog.get("started_at"), "catalog.started_at"),
            )
        return None
    started_at = _optional_wire_datetime(run.get("started_at"), "latest_run.started_at")
    ended_at = _optional_wire_datetime(run.get("ended_at"), "latest_run.ended_at")
    return SessionRunFacts(
        id=run_id,
        lifecycle="ended" if ended_at is not None else "running",
        started_at=started_at,
        ended_at=ended_at,
        end_reason=_text(run.get("exit_status")) if ended_at is not None else None,
    )


def _effective_head(
    heads: Collection[Mapping[str, Any]],
    *,
    session_id: str,
    family: Literal["activity", "control"],
    now: datetime,
) -> tuple[tuple[Mapping[str, Any], dict[str, Any], datetime, datetime] | None, int]:
    candidates: list[tuple[tuple[Any, ...], Mapping[str, Any], dict[str, Any], datetime, datetime]] = []
    rejected = 0
    for head in heads:
        if head.get("family") != family:
            continue
        try:
            value = _head_value(head, family=family, session_id=session_id)
            authority_class = value.get("authority_class")
            if not isinstance(authority_class, str):
                raise ValueError(f"unsupported {family} authority_class")
            rank = _AUTHORITY_RANK.get((family, authority_class))
            if rank is None:
                raise ValueError(f"unsupported {family} authority_class")
            observed_at = _wire_datetime(value.get("observed_at"), "observed_at")
            valid_until = _valid_until(family, head=head, value=value, observed_at=observed_at)
        except (TypeError, ValueError):
            rejected += 1
            continue
        if valid_until <= now:
            continue
        stable_coordinate = (
            str(head.get("source") or ""),
            str(head.get("source_epoch") or ""),
            str(head.get("evidence_hash") or ""),
        )
        candidates.append(((rank, observed_at, stable_coordinate), head, value, observed_at, valid_until))
    if not candidates:
        return None, rejected
    _key, head, value, observed_at, valid_until = max(candidates, key=lambda candidate: candidate[0])
    return (head, value, observed_at, valid_until), rejected


def _project_activity(
    winner: tuple[Mapping[str, Any], dict[str, Any], datetime, datetime] | None,
) -> SessionActivityFacts:
    if winner is None:
        return SessionActivityFacts(state="unknown")
    head, value, observed_at, valid_until = winner
    raw_kind = str(value.get("raw_kind") or value.get("kind") or "").strip() or None
    state = _ACTIVITY_STATE.get(str(value.get("kind") or ""), "unknown")
    return SessionActivityFacts(
        state=state,
        raw_kind=raw_kind,
        tool=str(value.get("tool_name") or "").strip() or None,
        source=str(head.get("source") or "").strip() or None,
        observed_at=observed_at,
        valid_until=valid_until,
    )


def _project_control(
    winner: tuple[Mapping[str, Any], dict[str, Any], datetime, datetime] | None,
    *,
    supported_operations: set[str],
) -> SessionControlFacts | None:
    if winner is None:
        return None
    head, value, observed_at, valid_until = winner
    raw_state = value.get("state")
    connection = {
        "attached": "connected",
        "degraded": "degraded",
        "detached": "disconnected",
    }.get(raw_state, "unknown")
    grants = set(_validated_grants(value))

    def action(name: str) -> SessionActionAvailability:
        operation = _ACTION_OPERATION[name]
        if operation not in supported_operations:
            return SessionActionAvailability(state="unavailable", reason="unsupported")
        if operation not in grants:
            return SessionActionAvailability(state="unavailable", reason="not_granted")
        if connection != "connected":
            return SessionActionAvailability(state="unavailable", reason="connection_unavailable")
        return SessionActionAvailability(state="available")

    return SessionControlFacts(
        ownership="owned",
        connection=connection,
        connection_id=str(value.get("connection_id") or "").strip() or None,
        lease_generation=str(value.get("lease_generation") or "").strip() or None,
        control_plane=None,
        observed_at=observed_at,
        valid_until=valid_until,
        actions=SessionControlActions(
            send_input=action("send_input"),
            interrupt=action("interrupt"),
            terminate=action("terminate"),
            reattach=SessionActionAvailability(state="unavailable", reason="unsupported"),
            resume=action("resume"),
        ),
    )


def _control_run_id(
    winner: tuple[Mapping[str, Any], dict[str, Any], datetime, datetime] | None,
) -> str | None:
    if winner is None:
        return None
    return _text(winner[1].get("run_id"))


def _head_diagnostic(
    family: Literal["activity", "control"],
    winner: tuple[Mapping[str, Any], dict[str, Any], datetime, datetime] | None,
) -> FactHeadDiagnostic | None:
    if winner is None:
        return None
    head, _value, observed_at, valid_until = winner
    updated_commit_seq = head.get("updated_commit_seq")
    return FactHeadDiagnostic(
        family=family,
        subject_key=str(head.get("subject_key") or ""),
        source=str(head.get("source") or ""),
        source_epoch=str(head.get("source_epoch") or ""),
        evidence_hash=str(head.get("evidence_hash") or ""),
        observed_at=observed_at,
        valid_until=valid_until,
        updated_commit_seq=updated_commit_seq if isinstance(updated_commit_seq, int) else None,
    )


def _valid_until(
    family: str,
    *,
    head: Mapping[str, Any],
    value: Mapping[str, Any],
    observed_at: datetime,
) -> datetime:
    if family == "control":
        ttl_ms = value.get("lease_ttl_ms")
        if type(ttl_ms) is not int or ttl_ms <= 0:
            raise ValueError("control lease_ttl_ms must be positive")
        return observed_at + timedelta(milliseconds=ttl_ms)
    raw = value.get("valid_until") or head.get("valid_until")
    return _wire_datetime(raw, "valid_until")


def _head_value(
    head: Mapping[str, Any],
    *,
    family: Literal["activity", "control"],
    session_id: str,
) -> dict[str, Any]:
    raw = head.get("value_json")
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("fact head value_json must contain an object")
    if head.get("session_id") != session_id:
        raise ValueError("fact head indexed session_id does not match the requested session")
    if value.get("session_id") != session_id:
        raise ValueError("fact head session_id does not match its value")
    source = head.get("source")
    if not isinstance(source, str) or not source.strip() or value.get("source") != source:
        raise ValueError("fact head source does not match its value")
    if canonical_evidence_hash(value) != head.get("evidence_hash"):
        raise ValueError("fact head evidence_hash does not match its value")
    if family == "activity":
        run_id = str(value.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("activity run_id is missing")
        expected_subject = f"run:{run_id}"
    else:
        connection_id = str(value.get("connection_id") or "").strip()
        lease_generation = str(value.get("lease_generation") or "").strip()
        if not connection_id or not lease_generation:
            raise ValueError("control connection identity is missing")
        expected_subject = f"connection:{connection_id}:{lease_generation}"
        _validated_grants(value)
    if head.get("subject_key") != expected_subject:
        raise ValueError("fact head subject_key does not match its value")
    return value


def _validated_grants(value: Mapping[str, Any]) -> list[str]:
    raw_grants = value.get("granted_operations")
    if not isinstance(raw_grants, list) or any(not isinstance(operation, str) for operation in raw_grants):
        raise ValueError("control granted_operations must be a string list")
    if raw_grants != sorted(set(raw_grants)) or any(operation not in _GRANTED_OPERATIONS for operation in raw_grants):
        raise ValueError("control granted_operations must be sorted, unique, and supported")
    return raw_grants


def _wire_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an RFC3339 timestamp")
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc


def _optional_wire_datetime(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    return _wire_datetime(value, field)


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


__all__ = [
    "ControlFactAuthorization",
    "FactHeadDiagnostic",
    "ShadowSessionStateProjection",
    "authorize_exact_control_fact",
    "project_served_session_state_facts",
    "project_shadow_session_state_facts",
]
