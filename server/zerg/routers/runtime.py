"""Runtime event ingest endpoints for Timeline runtime state."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.metrics import event_age_at_ingest_seconds
from zerg.models.agents import AgentSession
from zerg.services.apns_sender import WIDGET_PUSH_PLATFORM
from zerg.services.apns_sender import active_ios_targets_for_owner
from zerg.services.apns_sender import prepare_session_attention_push
from zerg.services.apns_sender import prepare_session_attention_resolution_push
from zerg.services.apns_sender import prepare_session_live_activity_pushes
from zerg.services.apns_sender import prepare_widget_timeline_push
from zerg.services.apns_sender import send_presence_pushes
from zerg.services.session_messages import deliver_queued_session_messages
from zerg.services.session_messages import is_session_message_deliverable_state
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventBatchResult
from zerg.services.session_runtime import current_presence_state_for_session
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.write_serializer import get_write_serializer

router = APIRouter(prefix="/agents/runtime", tags=["agents"])


@router.post("/events/batch", response_model=RuntimeEventBatchResult)
async def ingest_runtime_observation_batch(
    payload: RuntimeEventBatchIngest,
    db: Session = Depends(get_db),
    _token: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> RuntimeEventBatchResult:
    """Ingest normalized runtime observations and materialize runtime state."""
    try:
        ws = get_write_serializer()
        events = payload.events

        # Observation age at ingest: occurred_at (engine) -> now (server receive).
        # Codex bridge runtime observations are always managed.
        now_utc = datetime.now(timezone.utc)
        for ev in events:
            ev_ts = ev.occurred_at
            if ev_ts is None:
                continue
            if ev_ts.tzinfo is None:
                ev_ts = ev_ts.replace(tzinfo=timezone.utc)
            age_s = (now_utc - ev_ts).total_seconds()
            if age_s < 0:
                age_s = 0.0
            elif age_s > 3600:
                continue
            event_age_at_ingest_seconds.labels(
                surface="runtime",
                provider=ev.provider or "unknown",
                managed="true",
            ).observe(age_s)

        # Snapshot presence, ingest, and prepare APNs pushes atomically in one
        # write-serializer closure so the debounce stamps observed by prepare_*
        # match the state the ingest just committed. Mirrors presence.py.
        session_ids_in_batch = sorted({ev.session_id for ev in events if ev.session_id is not None}, key=str)
        # Pick first non-empty tool_name per session for attention push context.
        tool_by_session: dict = {}
        for ev in events:
            if ev.session_id is not None and ev.tool_name:
                tool_by_session.setdefault(ev.session_id, ev.tool_name)
        owner_id = resolve_session_message_owner_id(db, _token)

        def _do(wdb: Session):
            previous_by_session: dict = {}
            for sid in session_ids_in_batch:
                previous_by_session[sid] = current_presence_state_for_session(wdb, sid, now=now_utc)
            ingest_result = ingest_runtime_events(wdb, events)

            # Prepare per-session pushes on the post-ingest state.
            prepared: list[dict] = []
            widget_push = None
            if session_ids_in_batch:
                # Pre-fetch APNs target sets ONCE per (owner, platform) for the batch
                # rather than per-session × per-prep-fn. The widget timeline push is
                # owner-scoped (not session-scoped), so prepare it ONCE per batch.
                ios_targets = (
                    active_ios_targets_for_owner(wdb, owner_id=owner_id, log_context="runtime batch")
                    if owner_id is not None
                    else None
                )
                widget_targets = (
                    active_ios_targets_for_owner(
                        wdb,
                        owner_id=owner_id,
                        platform=WIDGET_PUSH_PLATFORM,
                        log_context="runtime batch widget",
                    )
                    if owner_id is not None
                    else None
                )
                widget_push = prepare_widget_timeline_push(
                    wdb,
                    owner_id=owner_id,
                    occurred_at=now_utc,
                    targets=widget_targets,
                )

                session_rows = wdb.query(AgentSession).filter(AgentSession.id.in_(session_ids_in_batch)).all()
                runtime_state_map = load_runtime_state_map(wdb, session_ids_in_batch)
                for session_row in session_rows:
                    canonical_state = resolve_runtime_overlay(
                        session_row,
                        last_activity_at=session_row.last_activity_at,
                        runtime_state_map=runtime_state_map,
                        now=now_utc,
                    ).presence_state
                    sid = session_row.id
                    tool = tool_by_session.get(sid)
                    prev = previous_by_session.get(sid)
                    prepared.append(
                        {
                            "session_id": sid,
                            "canonical_state": canonical_state,
                            "attention_push": prepare_session_attention_push(
                                wdb,
                                owner_id=owner_id,
                                session_id=sid,
                                previous_state=prev,
                                current_state=canonical_state,
                                occurred_at=now_utc,
                                current_tool_name=tool,
                                targets=ios_targets,
                            ),
                            "attention_resolution_push": prepare_session_attention_resolution_push(
                                wdb,
                                owner_id=owner_id,
                                session_id=sid,
                                previous_state=prev,
                                current_state=canonical_state,
                                occurred_at=now_utc,
                                targets=ios_targets,
                            ),
                            "live_activity_pushes": prepare_session_live_activity_pushes(
                                wdb,
                                owner_id=owner_id,
                                session_id=sid,
                                current_state=canonical_state,
                                current_tool_name=tool,
                                occurred_at=now_utc,
                            ),
                        }
                    )
            return ingest_result, prepared, widget_push

        result, prepared_per_session, widget_push = await ws.execute_or_direct(
            _do, db, label="runtime-observations"
        )

        # Publish per-session after a successful write; SSE subscribers wake directly.
        updated_runtime_keys = set(result.updated_runtime_keys)
        if updated_runtime_keys:
            from zerg.services.session_pubsub import publish_session_runtime_update

            session_ids_published: set[str] = set()
            for ev in events:
                if ev.session_id is None or ev.runtime_key not in updated_runtime_keys:
                    continue
                sid = str(ev.session_id)
                if sid in session_ids_published:
                    continue
                session_ids_published.add(sid)
                publish_session_runtime_update(
                    session_id=sid,
                    provider=ev.provider,
                    source=ev.source,
                )

        # Send pre-prepared APNs pushes + deliver queued messages, per session.
        # Per-session exception fence so one bad dispatch doesn't skip the rest.
        # The widget timeline push is owner-scoped and fires once per batch (on
        # the first session iteration); subsequent iterations pass widget_push=None.
        # If there are no prepared sessions but a widget push exists, send it standalone.
        if widget_push is not None and not prepared_per_session:
            try:
                await send_presence_pushes(
                    attention_push=None,
                    attention_resolution_push=None,
                    widget_push=widget_push,
                    live_activity_pushes=(),
                    db=db,
                    ws=ws,
                    dispatch_label_prefix="runtime",
                )
            except Exception:
                import logging

                logging.getLogger(__name__).exception("APNs widget dispatch failed; continuing")

        for index, item in enumerate(prepared_per_session):
            sid = item["session_id"]
            canonical_state = item["canonical_state"]
            try:
                await send_presence_pushes(
                    attention_push=item["attention_push"],
                    attention_resolution_push=item["attention_resolution_push"],
                    widget_push=widget_push if index == 0 else None,
                    live_activity_pushes=item["live_activity_pushes"],
                    db=db,
                    ws=ws,
                    dispatch_label_prefix="runtime",
                )
                if is_session_message_deliverable_state(canonical_state):
                    await deliver_queued_session_messages(
                        db=db,
                        owner_id=owner_id,
                        target_session_id=sid,
                        target_presence_state=canonical_state,
                    )
            except Exception:
                import logging

                logging.getLogger(__name__).exception("APNs dispatch failed for session %s; continuing batch", sid)

        return result
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ingest runtime observations",
        ) from exc
