"""Session presence ingest endpoint.

Receives real-time state signals from Claude Code hooks:
  - UserPromptSubmit  → state=thinking
  - PreToolUse        → state=running    (tool_name set)
  - PostToolUse       → state=thinking
  - Stop              → state=idle
  - PermissionRequest → state=blocked    (tool_name set — waiting on that tool)
  - Notification/idle_prompt        → state=needs_user
  - Notification/elicitation_dialog → state=needs_user
  - Notification/permission_prompt  → state=blocked

One row per session_id, upserted on each call. Stale rows (>10 min) are
treated as gone by the active sessions endpoint.

Auto-resume: only thinking/running signal genuine resumption of work and
auto-resume snoozed sessions. blocked/needs_user are pause states — the
user must come back deliberately.

Authentication: same X-Agents-Token / device token as ingest.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.user import User
from zerg.routers.agents import verify_agents_token
from zerg.services.oikos_operator_policy import get_operator_policy
from zerg.services.oikos_operator_policy import operator_master_switch_enabled
from zerg.services.oikos_service import invoke_oikos
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_ENQUEUED
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_FAILED
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_SUPPRESSED
from zerg.services.oikos_wakeup_ledger import append_wakeup
from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_STATES = {"thinking", "running", "idle", "needs_user", "blocked"}

# States that store tool_name (session is actively blocked on a specific tool)
_STATES_WITH_TOOL = {"running", "blocked"}

# States that trigger auto-resume of snoozed sessions (genuine work restart)
_AUTO_RESUME_STATES = {"thinking", "running"}

# States worth waking proactive Oikos for immediately.
_OPERATOR_WAKE_STATES = {"blocked", "needs_user"}
_OPERATOR_CONVERSATION_ID = "operator:main"


class PresenceIn(BaseModel):
    """Payload from a Claude Code hook."""

    session_id: str
    state: str  # thinking | running | idle | needs_user | blocked
    tool_name: Optional[str] = None
    cwd: Optional[str] = None
    provider: Optional[str] = "claude"


def _effective_tool_name(payload: PresenceIn, previous: SessionPresence | None) -> str | None:
    if payload.state not in _STATES_WITH_TOOL:
        return None
    if payload.state == "blocked" and payload.tool_name is None:
        return previous.tool_name if previous is not None else None
    return payload.tool_name


def _should_wake_operator(
    *,
    previous: SessionPresence | None,
    state: str,
    tool_name: str | None,
) -> bool:
    if state not in _OPERATOR_WAKE_STATES:
        return False
    if previous is None:
        return True
    if previous.state != state:
        return True
    return (previous.tool_name or None) != (tool_name or None)


def _resolve_owner_id(db: Session, token: object | None) -> int | None:
    owner_id = getattr(token, "owner_id", None)
    if owner_id is not None:
        return int(owner_id)

    owner = db.query(User.id).order_by(User.id).first()
    if owner is None:
        return None
    return int(owner[0])


def _build_operator_message(
    *,
    payload: PresenceIn,
    project: str | None,
    tool_name: str | None,
) -> str:
    lines = [
        "System/operator wakeup: a coding session may need attention.",
        "",
        f"Trigger: presence.{payload.state}",
        f"Session ID: {payload.session_id}",
    ]
    if payload.provider:
        lines.append(f"Provider: {payload.provider}")
    if project:
        lines.append(f"Project: {project}")
    if tool_name:
        lines.append(f"Tool: {tool_name}")
    if payload.cwd:
        lines.append(f"CWD: {payload.cwd}")
    lines.extend(
        [
            "",
            "Inspect the relevant session history, then decide whether to wait, continue, or escalate.",
            "Do nothing if no action is warranted.",
        ]
    )
    return "\n".join(lines)


def _build_operator_surface_payload(
    *,
    payload: PresenceIn,
    project: str | None,
    tool_name: str | None,
) -> dict[str, str | None]:
    return {
        "trigger_type": f"presence.{payload.state}",
        "conversation_id": _OPERATOR_CONVERSATION_ID,
        "session_id": payload.session_id,
        "provider": payload.provider or "claude",
        "project": project,
        "tool_name": tool_name,
        "cwd": payload.cwd,
    }


def _build_presence_wakeup_key(payload: PresenceIn, tool_name: str | None) -> str:
    normalized_tool = tool_name or "-"
    return f"presence:{payload.session_id}:{payload.state}:{normalized_tool}"


async def _maybe_invoke_operator_wakeup(
    *,
    db: Session,
    token: object | None,
    payload: PresenceIn,
    project: str | None,
    tool_name: str | None,
) -> None:
    if not operator_master_switch_enabled():
        return

    trigger_type = f"presence.{payload.state}"
    wakeup_payload = _build_operator_surface_payload(payload=payload, project=project, tool_name=tool_name)
    wakeup_key = _build_presence_wakeup_key(payload, tool_name)
    owner_id = _resolve_owner_id(db, token)
    if owner_id is None:
        append_wakeup(
            db,
            owner_id=None,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_SUPPRESSED,
            reason="no_owner",
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            payload=wakeup_payload,
        )
        db.commit()
        logger.debug("Skipping operator wakeup for session %s: no owner resolved", payload.session_id)
        return
    if not get_operator_policy(db, owner_id).enabled:
        append_wakeup(
            db,
            owner_id=owner_id,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_SUPPRESSED,
            reason="user_policy_disabled",
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            payload=wakeup_payload,
        )
        db.commit()
        logger.debug(
            "Skipping operator wakeup for session %s: operator mode disabled for owner %s",
            payload.session_id,
            owner_id,
        )
        return

    message = _build_operator_message(payload=payload, project=project, tool_name=tool_name)
    message_id = f"operator-presence-{payload.session_id}-{payload.state}-{uuid4()}"

    try:
        run_id = await invoke_oikos(
            owner_id,
            message,
            message_id,
            source="operator",
            surface_adapter=OperatorSurfaceAdapter(owner_id=owner_id),
            surface_payload=wakeup_payload,
        )
        append_wakeup(
            db,
            owner_id=owner_id,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_ENQUEUED,
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            run_id=run_id,
            payload=wakeup_payload,
        )
        db.commit()
    except Exception:
        append_wakeup(
            db,
            owner_id=owner_id,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_FAILED,
            reason="invoke_failed",
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            payload=wakeup_payload,
        )
        db.commit()
        logger.exception("Failed to invoke operator wakeup for session %s", payload.session_id)


@router.post("/presence", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def upsert_presence(
    payload: PresenceIn,
    request: Request,
    db: Session = Depends(get_db),
    _token: object = Depends(verify_agents_token),
) -> Response:
    """Upsert real-time presence state for a session."""
    if payload.state not in VALID_STATES:
        # Silently ignore unknown states rather than erroring hooks
        return

    project: Optional[str] = None
    if payload.cwd:
        project = os.path.basename(payload.cwd.rstrip("/"))

    previous = db.query(SessionPresence).filter(SessionPresence.session_id == payload.session_id).first()

    now = datetime.now(timezone.utc)

    insert_tool_name = payload.tool_name if payload.state in _STATES_WITH_TOOL else None

    # On conflict: blocked state preserves existing tool_name when the incoming
    # payload has none (Notification/permission_prompt doesn't carry tool context,
    # but a prior PermissionRequest already set the correct tool).
    # running always uses the new value; all other states clear it.
    if payload.state == "blocked" and payload.tool_name is None:
        update_tool_name = SessionPresence.tool_name  # keep existing
    elif payload.state in _STATES_WITH_TOOL:
        update_tool_name = payload.tool_name
    else:
        update_tool_name = None

    effective_tool_name = _effective_tool_name(payload, previous)
    should_wake_operator = _should_wake_operator(
        previous=previous,
        state=payload.state,
        tool_name=effective_tool_name,
    )

    stmt = (
        sqlite_insert(SessionPresence)
        .values(
            session_id=payload.session_id,
            state=payload.state,
            tool_name=insert_tool_name,
            cwd=payload.cwd,
            project=project,
            provider=payload.provider or "claude",
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["session_id"],
            set_={
                "state": payload.state,
                "tool_name": update_tool_name,
                "cwd": payload.cwd,
                "project": project,
                "updated_at": now,
            },
        )
    )
    db.execute(stmt)

    # Auto-resume snoozed sessions on genuine work-restart signals only.
    # blocked/needs_user are pause states — user must come back deliberately.
    if payload.state in _AUTO_RESUME_STATES:
        try:
            from uuid import UUID

            session_uuid = UUID(payload.session_id)
            db.query(AgentSession).filter(
                AgentSession.id == session_uuid,
                AgentSession.user_state == "snoozed",
            ).update(
                {"user_state": "active", "user_state_at": now},
                synchronize_session=False,
            )
        except (ValueError, AttributeError):
            pass  # session_id not a valid UUID — skip silently

    db.commit()
    if payload.state in _OPERATOR_WAKE_STATES and not should_wake_operator and operator_master_switch_enabled():
        owner_id = _resolve_owner_id(db, _token)
        if owner_id is not None and get_operator_policy(db, owner_id).enabled:
            wakeup_payload = _build_operator_surface_payload(
                payload=payload,
                project=project,
                tool_name=effective_tool_name,
            )
            append_wakeup(
                db,
                owner_id=owner_id,
                source="presence",
                trigger_type=f"presence.{payload.state}",
                status=WAKEUP_STATUS_SUPPRESSED,
                reason="duplicate_state",
                session_id=payload.session_id,
                conversation_id=_OPERATOR_CONVERSATION_ID,
                wakeup_key=_build_presence_wakeup_key(payload, effective_tool_name),
                payload=wakeup_payload,
            )
            db.commit()
    if should_wake_operator:
        await _maybe_invoke_operator_wakeup(
            db=db,
            token=_token,
            payload=payload,
            project=project,
            tool_name=effective_tool_name,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
