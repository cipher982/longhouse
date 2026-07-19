"""Pure shadow projection from bounded reducer heads.

Phase 3 deliberately projects only the activity and control axes represented
by schema-v3 machine evidence. Durable lifecycle, transcript, interaction,
host, and presentation facts remain explicitly unsupported until cutover.
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

from zerg.machine_evidence import canonical_evidence_hash
from zerg.services.session_state_contract import STATE_CONTRACT_VERSION
from zerg.services.session_state_contract import SessionActionAvailability
from zerg.services.session_state_contract import SessionActivityFacts
from zerg.services.session_state_contract import SessionControlActions
from zerg.services.session_state_contract import SessionControlFacts

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

SHADOW_SUPPORTED_FAMILIES: tuple[str, ...] = ("activity", "control")
SHADOW_UNSUPPORTED_FAMILIES: tuple[UnsupportedFactFamily, ...] = (
    "mode",
    "disposition",
    "launch",
    "run",
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
    """Non-served Phase 3 projection with unsupported axes named explicitly."""

    model_config = ConfigDict(frozen=True)

    state_contract_version: int = STATE_CONTRACT_VERSION
    commit_seq: int
    activity: SessionActivityFacts
    control: SessionControlFacts | None
    rejected_heads: int = 0
    unsupported_families: tuple[UnsupportedFactFamily, ...] = SHADOW_UNSUPPORTED_FAMILIES


def project_shadow_session_state_facts(
    *,
    session_id: str,
    commit_seq: int,
    heads: Collection[Mapping[str, Any]],
    supported_operations: Collection[str] = (),
    now: datetime,
) -> ShadowSessionStateProjection:
    """Project activity/control from one coherent catalog snapshot."""

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
    return ShadowSessionStateProjection(
        commit_seq=commit_seq,
        activity=_project_activity(activity_head),
        control=_project_control(control_head, supported_operations=set(supported_operations)),
        rejected_heads=rejected_activity + rejected_control,
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
    raw_grants = value.get("granted_operations")
    if not isinstance(raw_grants, list) or any(not isinstance(operation, str) for operation in raw_grants):
        raise ValueError("control granted_operations must be a string list")
    if raw_grants != sorted(set(raw_grants)) or any(operation not in _GRANTED_OPERATIONS for operation in raw_grants):
        raise ValueError("control granted_operations must be sorted, unique, and supported")
    grants = set(raw_grants)

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
        raw_grants = value.get("granted_operations")
        if not isinstance(raw_grants, list) or any(not isinstance(operation, str) for operation in raw_grants):
            raise ValueError("control granted_operations must be a string list")
        if raw_grants != sorted(set(raw_grants)) or any(operation not in _GRANTED_OPERATIONS for operation in raw_grants):
            raise ValueError("control granted_operations must be sorted, unique, and supported")
    if head.get("subject_key") != expected_subject:
        raise ValueError("fact head subject_key does not match its value")
    return value


def _wire_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an RFC3339 timestamp")
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


__all__ = ["ShadowSessionStateProjection", "project_shadow_session_state_facts"]
