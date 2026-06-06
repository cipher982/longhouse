"""Runtime event ingest endpoints for Timeline runtime state."""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.metrics import event_age_at_ingest_seconds
from zerg.models.agents import AgentSession
from zerg.services.apns_sender import WIDGET_PUSH_PLATFORM
from zerg.services.apns_sender import active_ios_targets_for_owner
from zerg.services.apns_sender import prepare_long_run_waiting_push
from zerg.services.apns_sender import prepare_session_attention_push
from zerg.services.apns_sender import prepare_session_attention_resolution_push
from zerg.services.apns_sender import prepare_session_blocked_reminder_push
from zerg.services.apns_sender import prepare_session_live_activity_pushes
from zerg.services.apns_sender import prepare_widget_timeline_push
from zerg.services.apns_sender import send_presence_pushes
from zerg.services.session_messages import deliver_queued_session_messages
from zerg.services.session_messages import is_session_message_deliverable_state
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventBatchResult
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.write_serializer import execute_post_write
from zerg.services.write_serializer import get_write_serializer
from zerg.services.write_serializer import post_write_db_session
from zerg.services.write_serializer import post_write_fallback_db

router = APIRouter(prefix="/agents/runtime", tags=["agents"])


@router.post("/events/batch", response_model=RuntimeEventBatchResult)
async def ingest_runtime_observation_batch(
    payload: RuntimeEventBatchIngest,
    response: Response,
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

        # Commit runtime state first, then run APNs/widget/queued-message prep
        # in a lower-priority follow-up write so SSE-visible state changes do
        # not sit behind notification bookkeeping.
        live_transcript_only = bool(events) and all(_is_bridge_live_transcript_event(ev) for ev in events)
        # Pick first non-empty tool_name per session for attention push context.
        tool_by_session: dict = {}
        for ev in events:
            if ev.session_id is not None and ev.tool_name:
                tool_by_session.setdefault(ev.session_id, ev.tool_name)

        if live_transcript_only:
            _publish_live_transcript_previews(events, now=now_utc)
        owner_id = resolve_session_message_owner_id(db, _token)

        def _do_runtime_state(wdb: Session):
            ingest_result = ingest_runtime_events(wdb, events)

            # Bridge live transcript deltas are already a user-visible overlay
            # with per-session SSE fanout. They must not pay the APNs/widget/
            # queued-message cost reserved for phase and attention changes.
            if live_transcript_only:
                return ingest_result, []

            updated_runtime_keys = set(ingest_result.updated_runtime_keys)
            if not updated_runtime_keys:
                return ingest_result, []

            updated_session_ids = sorted(
                {ev.session_id for ev in events if ev.session_id is not None and ev.runtime_key in updated_runtime_keys},
                key=str,
            )
            if not updated_session_ids:
                return ingest_result, []

            push_contexts = [
                {
                    "session_id": sid,
                    "tool": tool_by_session.get(sid),
                }
                for sid in updated_session_ids
            ]
            return ingest_result, push_contexts

        result, push_contexts = await ws.execute_after_closing_request_session(
            _do_runtime_state,
            db,
            label="runtime-live" if live_transcript_only else "runtime-observations",
        )
        from zerg.services.write_serializer import last_write_timing

        timing = last_write_timing()
        if timing is not None:
            response.headers["X-Runtime-Queue-Wait-Ms"] = f"{timing.queue_wait_ms:.1f}"
            response.headers["X-Runtime-Exec-Ms"] = f"{timing.exec_ms:.1f}"
            if timing.label:
                response.headers["X-Runtime-Label"] = timing.label

        # Publish per-session after a successful runtime-state write; SSE
        # subscribers should not wait behind APNs/widget/queued-message prep.
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

        prepared_per_session: list[dict] = []
        widget_push = None

        if push_contexts:
            push_context_by_session = {item["session_id"]: item for item in push_contexts}
            push_session_ids = list(push_context_by_session.keys())

            def _do_runtime_push_prep(wdb: Session):
                # Pre-fetch APNs target sets ONCE per (owner, platform) for the batch
                # rather than per-session × per-prep-fn. The widget timeline push is
                # owner-scoped (not session-scoped), so prepare it ONCE per changed batch.
                ios_targets = (
                    active_ios_targets_for_owner(wdb, owner_id=owner_id, log_context="runtime batch") if owner_id is not None else None
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
                next_widget_push = prepare_widget_timeline_push(
                    wdb,
                    owner_id=owner_id,
                    occurred_at=now_utc,
                    targets=widget_targets,
                )

                prepared: list[dict] = []
                session_rows = wdb.query(AgentSession).filter(AgentSession.id.in_(push_session_ids)).all()
                runtime_state_map = load_runtime_state_map(wdb, push_session_ids)
                for session_row in session_rows:
                    canonical_state = resolve_runtime_overlay(
                        session_row,
                        last_activity_at=session_row.last_activity_at,
                        runtime_state_map=runtime_state_map,
                        now=now_utc,
                    ).presence_state
                    sid = session_row.id
                    context = push_context_by_session.get(sid, {})
                    previous_attention_state = _previous_attention_state_from_session(session_row)
                    attention_push = prepare_session_attention_push(
                        wdb,
                        owner_id=owner_id,
                        session_id=sid,
                        previous_state=previous_attention_state,
                        current_state=canonical_state,
                        occurred_at=now_utc,
                        current_tool_name=context.get("tool"),
                        targets=ios_targets,
                    )
                    if attention_push is None:
                        attention_push = prepare_session_blocked_reminder_push(
                            wdb,
                            owner_id=owner_id,
                            session_id=sid,
                            current_state=canonical_state,
                            occurred_at=now_utc,
                            current_tool_name=context.get("tool"),
                            targets=ios_targets,
                        )
                    if attention_push is None:
                        attention_push = prepare_long_run_waiting_push(
                            wdb,
                            owner_id=owner_id,
                            session_id=sid,
                            current_state=canonical_state,
                            occurred_at=now_utc,
                            targets=ios_targets,
                        )
                    prepared.append(
                        {
                            "session_id": sid,
                            "canonical_state": canonical_state,
                            "attention_push": attention_push,
                            "attention_resolution_push": prepare_session_attention_resolution_push(
                                wdb,
                                owner_id=owner_id,
                                session_id=sid,
                                previous_state=previous_attention_state,
                                current_state=canonical_state,
                                occurred_at=now_utc,
                                targets=ios_targets,
                            ),
                            "live_activity_pushes": prepare_session_live_activity_pushes(
                                wdb,
                                owner_id=owner_id,
                                session_id=sid,
                                current_state=canonical_state,
                                current_tool_name=context.get("tool"),
                                occurred_at=now_utc,
                                runtime_state_map=runtime_state_map,
                            ),
                        }
                    )
                return prepared, next_widget_push

            prepared_per_session, widget_push = await execute_post_write(ws, _do_runtime_push_prep, db, label="runtime-push")

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
                    db=post_write_fallback_db(ws, db),
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
                    db=post_write_fallback_db(ws, db),
                    ws=ws,
                    dispatch_label_prefix="runtime",
                )
                if is_session_message_deliverable_state(canonical_state):
                    with post_write_db_session(ws, db) as dispatch_db:
                        await deliver_queued_session_messages(
                            db=dispatch_db,
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
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ingest runtime observations",
        ) from exc


def _is_bridge_live_transcript_event(event) -> bool:
    payload = event.payload or {}
    return (
        (event.provider or "").strip().lower() == "codex"
        and (event.source or "").strip().lower() == "codex_bridge_live"
        and event.kind == "progress_signal"
        and payload.get("progress_kind") == "bridge_live_transcript_delta"
    )


def _publish_live_transcript_previews(events, *, now: datetime) -> None:
    from zerg.services.session_pubsub import publish_session_transcript_preview_update

    latest_by_session: dict[str, tuple[object, dict]] = {}
    for event in events:
        preview = _live_transcript_preview_payload(event, now=now)
        if preview is None or event.session_id is None:
            continue
        sid = str(event.session_id)
        existing = latest_by_session.get(sid)
        if existing is not None and _preview_seq(preview) < _preview_seq(existing[1]):
            continue
        latest_by_session[sid] = (event, preview)

    logger = logging.getLogger("longhouse.live_transcript")
    for sid, (event, preview) in latest_by_session.items():
        publish_session_transcript_preview_update(
            session_id=sid,
            provider=event.provider,
            source=event.source,
            transcript_preview=preview,
        )
        observed_at = event.occurred_at
        if observed_at is not None:
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            age_ms = max(0.0, (now - observed_at).total_seconds() * 1000.0)
        else:
            age_ms = 0.0
        logger.info(
            "live_transcript publish session=%s seq=%s age_ms=%.1f text_len=%d complete=%s",
            sid,
            _preview_seq(preview),
            age_ms,
            len(preview.get("text") or ""),
            preview.get("is_complete"),
        )


def _live_transcript_preview_payload(event, *, now: datetime) -> dict | None:
    payload = event.payload or {}
    text = str(payload.get("live_text") or "").strip()
    if not text or event.session_id is None:
        return None

    seq = _coerce_nonnegative_int(payload.get("seq"))
    observed_at = event.occurred_at or now
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    else:
        observed_at = observed_at.astimezone(timezone.utc)

    thread_id = str(payload.get("thread_id") or "unknown-thread").strip() or "unknown-thread"
    turn_id = str(payload.get("turn_id") or "unknown-turn").strip() or "unknown-turn"
    cursor_seq = str(seq) if seq is not None else "unknown-seq"
    return {
        "event_id": seq or 0,
        "text": text,
        "event_origin": "live_provisional",
        "timestamp": observed_at.isoformat().replace("+00:00", "Z"),
        "is_provisional": True,
        "is_complete": bool(payload.get("turn_completed")),
        "content_cursor": f"codex_bridge_live:{event.session_id}:{thread_id}:{turn_id}:{cursor_seq}",
        "is_stale": False,
        "stale_reason": None,
    }


def _coerce_nonnegative_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _preview_seq(preview: dict) -> int:
    value = _coerce_nonnegative_int(preview.get("event_id"))
    return value if value is not None else -1


def _previous_attention_state_from_session(session: AgentSession) -> str | None:
    value = str(session.last_attention_push_state or "").strip()
    base = value.split(":", 1)[0]
    if base in {"blocked", "needs_user"}:
        return base
    return None
