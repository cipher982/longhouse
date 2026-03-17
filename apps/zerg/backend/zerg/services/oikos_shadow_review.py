"""Deterministic shadow reviews for live Oikos wakeups."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.services.oikos_autonomy_journeys import AutonomyContextPacket
from zerg.services.oikos_autonomy_journeys import AutonomyPolicy
from zerg.services.oikos_autonomy_journeys import AutonomyProposedAction
from zerg.services.oikos_autonomy_journeys import AutonomySessionSnapshot
from zerg.services.oikos_autonomy_journeys import AutonomyTrigger
from zerg.services.oikos_autonomy_journeys import baseline_shadow_decider
from zerg.services.oikos_operator_policy import OikosOperatorPolicy
from zerg.session_loop_mode import SessionLoopMode

_RELATED_SESSION_LIMIT = 3


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_loop_mode(value: str | None) -> SessionLoopMode:
    try:
        return SessionLoopMode(str(value or SessionLoopMode.MANUAL.value).strip())
    except ValueError:
        return SessionLoopMode.MANUAL


def _supports_resume(session: AgentSession) -> bool:
    return (session.provider or "").strip().lower() == "claude"


def _load_last_message_by_role(db: Session, session_id: str, role: str) -> str | None:
    event = (
        db.query(AgentEvent.content_text)
        .filter(
            AgentEvent.session_id == session_id,
            AgentEvent.role == role,
            AgentEvent.content_text.isnot(None),
        )
        .order_by(desc(AgentEvent.timestamp), desc(AgentEvent.id))
        .first()
    )
    if event is None:
        return None
    content = event[0]
    if content is None:
        return None
    text = str(content).strip()
    return text or None


def _serialize_snapshot(snapshot: AutonomySessionSnapshot) -> dict[str, Any]:
    return {
        "session_id": snapshot.session_id,
        "provider": snapshot.provider,
        "status": snapshot.status,
        "resumable": snapshot.resumable,
        "project": snapshot.project,
        "last_user_message": snapshot.last_user_message,
        "last_ai_message": snapshot.last_ai_message,
        "summary": snapshot.summary,
        "presence_state": snapshot.presence_state,
        "blocked_reason": snapshot.blocked_reason,
        "loop_mode": snapshot.loop_mode.value,
    }


def _serialize_action(action: AutonomyProposedAction) -> dict[str, Any]:
    return {
        "kind": action.kind,
        "target_session_id": action.target_session_id,
        "summary": action.summary,
        "payload": dict(action.payload or {}),
    }


def _build_session_snapshot(
    db: Session,
    session: AgentSession,
    presence: SessionPresence | None,
) -> AutonomySessionSnapshot:
    session_id = str(session.id)
    blocked_reason = None
    if presence is not None and presence.state == "blocked":
        if presence.tool_name:
            blocked_reason = f"Waiting on tool permission for {presence.tool_name}."
        else:
            blocked_reason = "Waiting on tool permission."

    return AutonomySessionSnapshot(
        session_id=session_id,
        provider=str(session.provider or "").strip(),
        status="completed" if session.ended_at else "working",
        resumable=_supports_resume(session),
        project=str(session.project).strip() if session.project else None,
        last_user_message=_load_last_message_by_role(db, session_id, "user"),
        last_ai_message=_load_last_message_by_role(db, session_id, "assistant"),
        summary=str(session.summary).strip() if session.summary else None,
        presence_state=presence.state if presence is not None else None,
        blocked_reason=blocked_reason,
        loop_mode=_coerce_loop_mode(getattr(session, "loop_mode", None)),
    )


def _build_policy(policy: OikosOperatorPolicy) -> AutonomyPolicy:
    return AutonomyPolicy(
        shadow_mode=policy.shadow_mode,
        allow_continue=policy.allow_continue,
        allow_notify=policy.allow_notify,
        allow_small_repairs=policy.allow_small_repairs,
    )


def _load_related_snapshots(
    db: Session,
    primary_session: AgentSession,
) -> list[AutonomySessionSnapshot]:
    if not primary_session.project:
        return []

    sessions = (
        db.query(AgentSession)
        .filter(
            AgentSession.project == primary_session.project,
            AgentSession.id != primary_session.id,
            AgentSession.user_state != "archived",
            AgentSession.is_sidechain == 0,
        )
        .order_by(desc(AgentSession.started_at))
        .limit(_RELATED_SESSION_LIMIT)
        .all()
    )
    if not sessions:
        return []

    session_ids = [str(item.id) for item in sessions]
    presences = db.query(SessionPresence).filter(SessionPresence.session_id.in_(session_ids)).all()
    presence_map = {row.session_id: row for row in presences}
    return [_build_session_snapshot(db, item, presence_map.get(str(item.id))) for item in sessions]


async def build_session_shadow_review(
    db: Session,
    *,
    trigger_type: str,
    session_id: str,
    trigger_summary: str | None,
    trigger_payload: dict[str, Any] | None,
    policy: OikosOperatorPolicy,
) -> dict[str, Any] | None:
    """Build a deterministic shadow review for one live operator wakeup."""

    if not policy.shadow_mode:
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    presence = db.query(SessionPresence).filter(SessionPresence.session_id == session_id).first()
    primary_snapshot = _build_session_snapshot(db, session, presence)
    packet = AutonomyContextPacket(
        case_id=f"live:{trigger_type}:{session_id}",
        description=f"Live operator wakeup review for {trigger_type}",
        trigger=AutonomyTrigger(
            type=trigger_type,
            source_session_id=session_id,
            summary=trigger_summary,
            payload=dict(trigger_payload or {}),
        ),
        primary_session=primary_snapshot,
        active_sessions=_load_related_snapshots(db, session),
        policy=_build_policy(policy),
        artifacts=[],
    )
    decision = await baseline_shadow_decider(packet)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision": {
            "decision": decision.decision,
            "summary": decision.summary,
            "rationale": decision.rationale,
            "needs_human": decision.needs_human,
            "proposed_actions": [_serialize_action(action) for action in decision.proposed_actions],
        },
        "context": {
            "trigger": {
                "type": packet.trigger.type,
                "source_session_id": packet.trigger.source_session_id,
                "summary": packet.trigger.summary,
                "payload": dict(packet.trigger.payload or {}),
            },
            "primary_session": _serialize_snapshot(packet.primary_session),
            "active_sessions": [_serialize_snapshot(item) for item in packet.active_sessions],
            "policy": {
                "shadow_mode": packet.policy.shadow_mode,
                "allow_continue": packet.policy.allow_continue,
                "allow_notify": packet.policy.allow_notify,
                "allow_small_repairs": packet.policy.allow_small_repairs,
                "cadence_minutes": packet.policy.cadence_minutes,
            },
        },
    }
