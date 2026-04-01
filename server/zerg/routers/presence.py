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
from uuid import UUID
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.database import get_db
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.user import User
from zerg.services.oikos_operator_policy import get_operator_policy
from zerg.services.oikos_operator_policy import operator_master_switch_enabled
from zerg.services.oikos_service import invoke_oikos
from zerg.services.oikos_shadow_review import build_session_shadow_review
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_ENQUEUED
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_FAILED
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_SUPPRESSED
from zerg.services.oikos_wakeup_ledger import append_wakeup
from zerg.services.presence_cache import get_presence_cache
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import coerce_session_uuid
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.write_serializer import get_write_serializer
from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_STATES = {"thinking", "running", "idle", "needs_user", "blocked"}

# States that store tool_name (session is actively blocked on a specific tool)
_STATES_WITH_TOOL = {"running", "blocked"}

# States that trigger auto-resume of snoozed sessions (genuine work restart)
_AUTO_RESUME_STATES = {"thinking", "running"}

# States worth waking proactive Oikos for immediately.
# Completed-turn handling now runs off transcript ingest; keep presence wakeups
# only for true live interrupts such as permission blocks.
_OPERATOR_WAKE_STATES = {"blocked"}
_OPERATOR_CONVERSATION_ID = "operator:main"


class PresenceIn(UTCBaseModel):
    """Payload from a Claude Code hook."""

    session_id: str
    state: str  # thinking | running | idle | needs_user | blocked
    tool_name: Optional[str] = None
    cwd: Optional[str] = None
    provider: Optional[str] = "claude"
    occurred_at: Optional[datetime] = None
    dedupe_key: Optional[str] = None


def _effective_tool_name(payload: PresenceIn, previous: object | None) -> str | None:
    """Resolve effective tool name. `previous` can be SessionPresence or PresenceEntry."""
    if payload.state not in _STATES_WITH_TOOL:
        return None
    if payload.state == "blocked" and payload.tool_name is None:
        return getattr(previous, "tool_name", None) if previous is not None else None
    return payload.tool_name


def _should_wake_operator(
    *,
    previous: object | None,
    state: str,
    tool_name: str | None,
) -> bool:
    """Check if operator should be woken. `previous` can be SessionPresence or PresenceEntry."""
    if state not in _OPERATOR_WAKE_STATES:
        return False
    if previous is None:
        return True
    if getattr(previous, "state", None) != state:
        return True
    return (getattr(previous, "tool_name", None) or None) != (tool_name or None)


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
        "System/operator interrupt: a coding session paused and may need attention.",
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


async def _persist_wakeup(fallback_db: Session, **kwargs: object) -> None:
    """Write a wakeup ledger row via the serializer (or fallback session)."""
    ws = get_write_serializer()

    def _do(wdb: Session) -> None:
        append_wakeup(wdb, **kwargs)  # type: ignore[arg-type]

    await ws.execute_or_direct(_do, fallback_db, label="wakeup-ledger")


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
    surface_payload = _build_operator_surface_payload(payload=payload, project=project, tool_name=tool_name)
    wakeup_key = _build_presence_wakeup_key(payload, tool_name)
    owner_id = _resolve_owner_id(db, token)
    if owner_id is None:
        await _persist_wakeup(
            db,
            owner_id=None,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_SUPPRESSED,
            reason="no_owner",
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            payload=surface_payload,
        )
        logger.debug("Skipping operator wakeup for session %s: no owner resolved", payload.session_id)
        return
    policy = get_operator_policy(db, owner_id)
    if not policy.enabled:
        await _persist_wakeup(
            db,
            owner_id=owner_id,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_SUPPRESSED,
            reason="user_policy_disabled",
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            payload=surface_payload,
        )
        logger.debug(
            "Skipping operator wakeup for session %s: operator mode disabled for owner %s",
            payload.session_id,
            owner_id,
        )
        return

    ledger_payload = dict(surface_payload)
    if policy.shadow_mode:
        try:
            shadow_review = await build_session_shadow_review(
                db,
                trigger_type=trigger_type,
                session_id=payload.session_id,
                trigger_summary=f"Operator interrupt from {trigger_type}.",
                trigger_payload=surface_payload,
                policy=policy,
            )
            if shadow_review is not None:
                ledger_payload["shadow_review"] = shadow_review
        except Exception:
            logger.exception("Failed to build shadow review for presence wakeup on session %s", payload.session_id)

    message = _build_operator_message(payload=payload, project=project, tool_name=tool_name)
    message_id = str(uuid4())

    try:
        run_id = await invoke_oikos(
            owner_id,
            message,
            message_id,
            source="operator",
            surface_adapter=OperatorSurfaceAdapter(owner_id=owner_id),
            surface_payload=ledger_payload,
        )
        await _persist_wakeup(
            db,
            owner_id=owner_id,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_ENQUEUED,
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            run_id=run_id,
            payload=ledger_payload,
        )
    except Exception:
        await _persist_wakeup(
            db,
            owner_id=owner_id,
            source="presence",
            trigger_type=trigger_type,
            status=WAKEUP_STATUS_FAILED,
            reason="invoke_failed",
            session_id=payload.session_id,
            conversation_id=_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            payload=ledger_payload,
        )
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
    if isinstance(_token, ManagedLocalHookToken) and payload.session_id != _token.session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managed-local hook token does not match session",
        )

    project: Optional[str] = None
    if payload.cwd:
        project = os.path.basename(payload.cwd.rstrip("/"))

    now = payload.occurred_at.astimezone(timezone.utc) if payload.occurred_at is not None else datetime.now(timezone.utc)

    # Resolve tool_name with blocked-state preservation logic
    if payload.state == "blocked" and payload.tool_name is None:
        cache = get_presence_cache()
        prev_entry = cache.get(payload.session_id)
        insert_tool_name = getattr(prev_entry, "tool_name", None)
    elif payload.state in _STATES_WITH_TOOL:
        insert_tool_name = payload.tool_name
    else:
        insert_tool_name = None

    # In-memory upsert — no DB write. Flushed to SQLite every 5s.
    cache = get_presence_cache()
    _entry, prev_snapshot = cache.upsert(
        payload.session_id,
        payload.state,
        tool_name=insert_tool_name,
        device_id=getattr(_token, "device_id", None),
        cwd=payload.cwd,
        project=project,
        provider=payload.provider or "claude",
        updated_at=now,
    )

    effective_tool_name = _effective_tool_name(payload, prev_snapshot)
    should_wake_operator = _should_wake_operator(
        previous=prev_snapshot,
        state=payload.state,
        tool_name=effective_tool_name,
    )

    runtime_provider = payload.provider or "claude"
    runtime_key = runtime_key_for_session(runtime_provider, payload.session_id)
    runtime_dedupe_key = payload.dedupe_key or (
        f"presence:{payload.session_id}:{payload.state}:{effective_tool_name or '-'}:{now.isoformat()}"
    )
    # Build runtime event for serialized write
    runtime_event = RuntimeEventIngest(
        runtime_key=runtime_key,
        session_id=coerce_session_uuid(payload.session_id),
        provider=runtime_provider,
        device_id=getattr(_token, "device_id", None),
        source="claude_hook",
        kind="phase_signal",
        phase=payload.state,
        tool_name=effective_tool_name,
        occurred_at=now,
        freshness_ms=phase_freshness_ms(payload.state),
        dedupe_key=runtime_dedupe_key,
        payload={},
    )

    # Bundle runtime-event ingest + auto-resume into one serialized write.
    auto_resume = payload.state in _AUTO_RESUME_STATES
    _session_id_str = payload.session_id
    _now = now

    def _do_presence_writes(write_db: Session) -> None:
        ingest_runtime_events(write_db, [runtime_event])
        if auto_resume:
            try:
                session_uuid = UUID(_session_id_str)
                write_db.query(AgentSession).filter(
                    AgentSession.id == session_uuid,
                    AgentSession.user_state == "snoozed",
                ).update(
                    {"user_state": "active", "user_state_at": _now},
                    synchronize_session=False,
                )
            except (ValueError, AttributeError):
                pass

    ws = get_write_serializer()
    await ws.execute_or_direct(_do_presence_writes, db, label="presence")

    if payload.state in _OPERATOR_WAKE_STATES and not should_wake_operator and operator_master_switch_enabled():
        owner_id = _resolve_owner_id(db, _token)
        if owner_id is not None and get_operator_policy(db, owner_id).enabled:
            await _persist_wakeup(
                db,
                owner_id=owner_id,
                source="presence",
                trigger_type=f"presence.{payload.state}",
                status=WAKEUP_STATUS_SUPPRESSED,
                reason="duplicate_state",
                session_id=payload.session_id,
                conversation_id=_OPERATOR_CONVERSATION_ID,
                wakeup_key=_build_presence_wakeup_key(payload, effective_tool_name),
                payload=_build_operator_surface_payload(
                    payload=payload,
                    project=project,
                    tool_name=effective_tool_name,
                ),
            )
    if should_wake_operator:
        await _maybe_invoke_operator_wakeup(
            db=db,
            token=_token,
            payload=payload,
            project=project,
            tool_name=effective_tool_name,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
