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

Stage 4: `/api/agents/presence` is now a pure RuntimeEventIngest emitter.
Each POST normalizes the payload into a phase_signal and feeds it through
`ingest_runtime_events`, which materializes SessionRuntimeState via the
reducer. The legacy SessionPresence TTL cache is gone — SessionRuntimeState
is the single server-side runtime source of truth. The endpoint still
handles auto-resume of snoozed sessions and queued-message delivery.

Auto-resume: only thinking/running signal genuine resumption of work and
auto-resume snoozed sessions. blocked/needs_user are pause states — the
user must come back deliberately.

Authentication: same X-Agents-Token / device token as ingest.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Optional
from uuid import UUID

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
from zerg.services.session_messages import deliver_queued_session_messages
from zerg.services.session_messages import is_session_message_deliverable_state
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import coerce_session_uuid
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.write_serializer import get_write_serializer
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_STATES = {"thinking", "running", "idle", "needs_user", "blocked"}

# States that trigger auto-resume of snoozed sessions (genuine work restart)
_AUTO_RESUME_STATES = {"thinking", "running"}


class PresenceIn(UTCBaseModel):
    """Payload from a Claude Code hook."""

    session_id: str
    state: str  # thinking | running | idle | needs_user | blocked
    tool_name: Optional[str] = None
    cwd: Optional[str] = None
    provider: Optional[str] = "claude"
    occurred_at: Optional[datetime] = None
    dedupe_key: Optional[str] = None


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
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if isinstance(_token, ManagedLocalHookToken) and payload.session_id != _token.session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managed-local hook token does not match session",
        )

    now = payload.occurred_at.astimezone(timezone.utc) if payload.occurred_at is not None else datetime.now(timezone.utc)

    runtime_provider = payload.provider or "claude"
    runtime_key = runtime_key_for_session(runtime_provider, payload.session_id)
    # `blocked` signals sometimes arrive without a tool_name; the reducer
    # preserves the prior active_tool in that case, so we just pass the
    # event's tool_name as-is.
    runtime_tool_name = payload.tool_name if payload.state in {"running", "blocked"} else None
    runtime_dedupe_key = payload.dedupe_key or (
        f"presence:{payload.session_id}:{payload.state}:{runtime_tool_name or '-'}:{now.isoformat()}"
    )
    runtime_event = RuntimeEventIngest(
        runtime_key=runtime_key,
        session_id=coerce_session_uuid(payload.session_id),
        provider=runtime_provider,
        device_id=getattr(_token, "device_id", None),
        source="claude_hook",
        kind="phase_signal",
        phase=payload.state,
        tool_name=runtime_tool_name,
        occurred_at=now,
        freshness_ms=phase_freshness_ms(payload.state),
        dedupe_key=runtime_dedupe_key,
        payload={},
    )

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

    if is_session_message_deliverable_state(payload.state):
        try:
            session_uuid = UUID(payload.session_id)
        except ValueError:
            session_uuid = None
        if session_uuid is not None:
            await deliver_queued_session_messages(
                db=db,
                owner_id=resolve_session_message_owner_id(db, _token),
                target_session_id=session_uuid,
                target_presence_state=payload.state,
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
