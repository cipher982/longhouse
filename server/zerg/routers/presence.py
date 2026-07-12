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
import re
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
from zerg.database import catalog_db_dependency
from zerg.database import live_store_configured
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.services.apns_sender import NOTIFICATION_CHANNEL_APNS_IOS
from zerg.services.apns_sender import clear_live_activity_push_stamp
from zerg.services.apns_sender import clear_session_attention_resolution_stamp
from zerg.services.apns_sender import clear_widget_timeline_push_stamp
from zerg.services.apns_sender import prepare_session_attention_push
from zerg.services.apns_sender import prepare_session_attention_resolution_push
from zerg.services.apns_sender import prepare_session_blocked_reminder_push
from zerg.services.apns_sender import prepare_session_live_activity_pushes
from zerg.services.apns_sender import prepare_widget_timeline_push
from zerg.services.apns_sender import record_notification_delivery_result
from zerg.services.apns_sender import rollback_session_attention_push_stamp
from zerg.services.apns_sender import send_session_attention_push
from zerg.services.apns_sender import send_session_attention_resolution_push
from zerg.services.apns_sender import send_session_live_activity_push
from zerg.services.apns_sender import send_widget_timeline_push
from zerg.services.session_messages import deliver_queued_session_messages
from zerg.services.session_messages import is_session_message_deliverable_state
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventBatchResult
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import coerce_session_uuid
from zerg.services.session_runtime import current_presence_state_for_session
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.write_backpressure import raise_hot_write_backpressure
from zerg.services.write_serializer import WriteQueueTimeoutError
from zerg.services.write_serializer import execute_post_write
from zerg.services.write_serializer import get_write_serializer
from zerg.services.write_serializer import post_write_db_session
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)
_catalog_db_dependency = catalog_db_dependency()

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_STATES = {"thinking", "running", "idle", "needs_user", "blocked"}
_HOT_PRESENCE_QUEUE_TIMEOUT_SECONDS = 2.0

# States that trigger auto-resume of snoozed sessions (genuine work restart)
_AUTO_RESUME_STATES = {"thinking", "running"}


def _source_for_provider_hook(provider: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", (provider or "claude").strip().lower()).strip("_")
    if not normalized:
        normalized = "claude"
    return f"{normalized[:58]}_hook"


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
    db: Session = Depends(_catalog_db_dependency),
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

    if payload.occurred_at is not None:
        now = payload.occurred_at.astimezone(timezone.utc)
    else:
        now = datetime.now(timezone.utc)

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
        source=_source_for_provider_hook(runtime_provider),
        kind="phase_signal",
        phase=payload.state,
        tool_name=runtime_tool_name,
        occurred_at=now,
        freshness_ms=phase_freshness_ms(payload.state),
        dedupe_key=runtime_dedupe_key,
        payload={},
    )

    auto_resume = payload.state in _AUTO_RESUME_STATES
    _now = now
    session_uuid: UUID | None
    try:
        session_uuid = UUID(payload.session_id)
    except ValueError:
        session_uuid = None

    if live_store_configured():
        from zerg.routers.runtime import ingest_runtime_observation_batch

        runtime_response = Response()
        await ingest_runtime_observation_batch(
            RuntimeEventBatchIngest(events=[runtime_event]),
            runtime_response,
            db,
            _token,
            None,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=dict(runtime_response.headers))

    owner_id = resolve_session_message_owner_id(db, _token)

    def _do_presence_writes(write_db: Session):
        if session_uuid is not None:
            previous_presence_state = current_presence_state_for_session(write_db, session_uuid, now=_now)
        else:
            previous_presence_state = None
        ingest_result: RuntimeEventBatchResult = ingest_runtime_events(write_db, [runtime_event])
        canonical_presence_state = (
            current_presence_state_for_session(write_db, session_uuid, now=_now) if session_uuid is not None else None
        )
        if (
            auto_resume
            and runtime_key in ingest_result.updated_runtime_keys
            and canonical_presence_state in _AUTO_RESUME_STATES
            and session_uuid is not None
        ):
            write_db.query(AgentSession).filter(
                AgentSession.id == session_uuid,
                AgentSession.user_state == "snoozed",
            ).update(
                {"user_state": "active", "user_state_at": _now},
                synchronize_session=False,
            )
        attention_push = prepare_session_attention_push(
            write_db,
            owner_id=owner_id,
            session_id=session_uuid,
            previous_state=previous_presence_state,
            current_state=canonical_presence_state,
            occurred_at=_now,
            current_tool_name=runtime_tool_name,
        )
        if attention_push is None:
            attention_push = prepare_session_blocked_reminder_push(
                write_db,
                owner_id=owner_id,
                session_id=session_uuid,
                current_state=canonical_presence_state,
                occurred_at=_now,
                current_tool_name=runtime_tool_name,
            )
        attention_resolution_push = prepare_session_attention_resolution_push(
            write_db,
            owner_id=owner_id,
            session_id=session_uuid,
            previous_state=previous_presence_state,
            current_state=canonical_presence_state,
            occurred_at=_now,
        )
        widget_push = prepare_widget_timeline_push(
            write_db,
            owner_id=owner_id,
            occurred_at=_now,
        )
        live_activity_pushes = prepare_session_live_activity_pushes(
            write_db,
            owner_id=owner_id,
            session_id=session_uuid,
            current_state=canonical_presence_state,
            current_tool_name=runtime_tool_name,
            occurred_at=_now,
        )
        should_publish_runtime_update = runtime_key in ingest_result.updated_runtime_keys
        return (
            canonical_presence_state,
            should_publish_runtime_update,
            attention_push,
            attention_resolution_push,
            widget_push,
            live_activity_pushes,
        )

    ws = get_write_serializer()
    try:
        (
            canonical_presence_state,
            should_publish_runtime_update,
            attention_push,
            attention_resolution_push,
            widget_push,
            live_activity_pushes,
        ) = await ws.execute_after_closing_request_session(
            _do_presence_writes,
            db,
            label="presence",
            queue_timeout_seconds=_HOT_PRESENCE_QUEUE_TIMEOUT_SECONDS,
        )
    except WriteQueueTimeoutError:
        raise_hot_write_backpressure(ws, admission_state="presence_queue_timeout")

    if session_uuid is not None and should_publish_runtime_update:
        from zerg.services.session_pubsub import publish_session_runtime_update

        publish_session_runtime_update(
            session_id=str(session_uuid),
            provider=runtime_event.provider,
            source=runtime_event.source,
        )

    if session_uuid is not None and is_session_message_deliverable_state(canonical_presence_state):
        with post_write_db_session(ws, db) as delivery_db:
            await deliver_queued_session_messages(
                db=delivery_db,
                owner_id=owner_id,
                target_session_id=session_uuid,
                target_presence_state=canonical_presence_state,
            )
            from zerg.services.session_input_queue import wake_session_input_queue

            await wake_session_input_queue(
                db_bind=delivery_db.get_bind(),
                session_id=session_uuid,
                reason="presence_runtime_deliverable",
            )
    if attention_push is not None:
        push_sent = False
        try:
            push_sent = await send_session_attention_push(attention_push)
        except Exception:  # pragma: no cover - push send should never fail the hook path
            logger.exception("Failed to send APNs attention push for session %s", attention_push.session_id)

        def _record_attention_result(write_db: Session) -> bool:
            return record_notification_delivery_result(
                write_db,
                event_id=attention_push.notification_event_id,
                channel=NOTIFICATION_CHANNEL_APNS_IOS,
                accepted=push_sent,
                occurred_at=attention_push.occurred_at,
            )

        await execute_post_write(ws, _record_attention_result, db, label="presence-attention-record")
        if not push_sent:

            def _clear_attention_push_stamp(write_db: Session):
                rollback_session_attention_push_stamp(write_db, notification=attention_push)

            await execute_post_write(ws, _clear_attention_push_stamp, db, label="presence-attention-push-clear")
    if attention_resolution_push is not None:
        try:
            resolution_accepted = await send_session_attention_resolution_push(attention_resolution_push)
        except Exception:  # pragma: no cover - push send should never fail the hook path
            logger.exception("Failed to send APNs attention resolution push for session %s", attention_resolution_push.session_id)
        else:
            if not resolution_accepted:

                def _clear_attention_resolution_stamp(write_db: Session) -> bool:
                    return clear_session_attention_resolution_stamp(
                        write_db,
                        session_id=attention_resolution_push.session_id,
                        state=attention_resolution_push.previous_state,
                        attention_push_at=attention_resolution_push.attention_push_at,
                    )

                await execute_post_write(
                    ws,
                    _clear_attention_resolution_stamp,
                    db,
                    label="presence-attention-resolution-clear",
                )
    if widget_push is not None:
        try:
            widget_accepted = await send_widget_timeline_push(widget_push)
        except Exception:  # pragma: no cover - push send should never fail the hook path
            logger.exception("Failed to send APNs widget timeline push for user %s", widget_push.owner_id)
        else:
            if not widget_accepted:

                def _clear_widget_timeline_stamp(write_db: Session) -> bool:
                    return clear_widget_timeline_push_stamp(
                        write_db,
                        owner_id=widget_push.owner_id,
                        state_hash=widget_push.state_hash,
                        previous_state_hash=widget_push.previous_state_hash,
                        previous_push_at=widget_push.previous_push_at,
                    )

                await execute_post_write(ws, _clear_widget_timeline_stamp, db, label="presence-widget-push-clear")
    for live_activity_push in live_activity_pushes:
        try:
            live_activity_accepted = await send_session_live_activity_push(live_activity_push)
        except Exception:  # pragma: no cover - push send should never fail the hook path
            logger.exception(
                "Failed to send APNs Live Activity push for session %s",
                live_activity_push.session_id,
            )
            live_activity_accepted = False
        if not live_activity_accepted:

            def _clear_live_activity_stamp(write_db: Session, push=live_activity_push) -> bool:
                return clear_live_activity_push_stamp(
                    write_db,
                    registration_id=push.registration_id,
                    state_hash=push.state_hash,
                    previous_state_hash=push.previous_state_hash,
                    previous_push_at=push.previous_push_at,
                )

            await execute_post_write(ws, _clear_live_activity_stamp, db, label="presence-live-activity-clear")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
