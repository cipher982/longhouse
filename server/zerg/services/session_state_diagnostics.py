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


class SessionStateComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["matched", "different", "not_comparable"]
    same_commit: bool
    activity: SessionStateAxisComparison | None = None
    control: SessionStateAxisComparison | None = None


def compare_session_state_axes(
    *,
    legacy: SessionStateFacts,
    shadow: ShadowSessionStateProjection,
    legacy_commit_seq: int,
    shadow_commit_seq: int,
) -> SessionStateComparison:
    """Compare only axes Phase 3 can project, never presentation or lifecycle."""

    if legacy_commit_seq != shadow_commit_seq:
        return SessionStateComparison(status="not_comparable", same_commit=False)

    activity = _axis(_activity_payload(legacy.activity), _activity_payload(shadow.activity))
    control = _axis(_control_payload(legacy.control), _control_payload(shadow.control))
    return SessionStateComparison(
        status="matched" if activity.matches and control.matches else "different",
        same_commit=True,
        activity=activity,
        control=control,
    )


def _axis(legacy: dict[str, Any] | None, shadow: dict[str, Any] | None) -> SessionStateAxisComparison:
    return SessionStateAxisComparison(matches=legacy == shadow, legacy=legacy, shadow=shadow)


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


__all__ = [
    "SessionStateAxisComparison",
    "SessionStateComparison",
    "compare_session_state_axes",
]
