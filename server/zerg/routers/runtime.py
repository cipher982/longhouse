"""Runtime event ingest endpoints for Timeline runtime state."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.database import catalog_db_dependency
from zerg.database import live_store_configured
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.metrics import event_age_at_ingest_seconds
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventBatchResult
from zerg.services.session_runtime import _is_bridge_transcript_event

router = APIRouter(prefix="/agents/runtime", tags=["agents"])
_catalog_db_dependency = catalog_db_dependency()

_HOT_RUNTIME_QUEUE_TIMEOUT_SECONDS = 2.0


def _no_runtime_db():
    """The hosted Runtime Host delegates runtime-state storage to catalogd."""

    yield None


_settings = get_settings()
_runtime_db_dependency = (
    _catalog_db_dependency
    if _settings.testing or os.getenv("TESTING", "").strip().lower() in {"1", "true", "yes", "on"} or not live_store_configured()
    else _no_runtime_db
)


@router.post("/events/batch", response_model=RuntimeEventBatchResult)
async def ingest_runtime_observation_batch(
    payload: RuntimeEventBatchIngest,
    response: Response,
    db: Session | None = Depends(_runtime_db_dependency),
    _token: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> RuntimeEventBatchResult:
    """Ingest normalized runtime observations and materialize runtime state."""
    try:
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

        live_transcript_only = bool(events) and all(_is_bridge_live_transcript_event(ev) for ev in events)

        if live_transcript_only:
            _publish_live_transcript_previews(events, now=now_utc)

        def _publish_runtime_updates(
            result: RuntimeEventBatchResult,
            *,
            catalog_commit_seq: str | int | None = None,
        ) -> None:
            updated_runtime_keys = set(result.updated_runtime_keys)
            if not updated_runtime_keys:
                return

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
                    catalog_commit_seq=catalog_commit_seq,
                )

        catalogd = get_catalogd_client()
        if catalogd is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            )
        try:
            raw_result = await catalogd.call(
                "session.runtime.apply.v2",
                {"events": [event.model_dump(mode="json") for event in events]},
                timeout_seconds=_HOT_RUNTIME_QUEUE_TIMEOUT_SECONDS,
            )
        except CatalogUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            ) from exc
        except CatalogRemoteError as exc:
            raise HTTPException(
                status_code=(status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_500_INTERNAL_SERVER_ERROR),
                detail={
                    "code": "catalog_unavailable" if exc.retryable else "catalog_operation_failed",
                    "message": ("Catalog mutation is temporarily unavailable." if exc.retryable else "Catalog runtime mutation failed."),
                },
            ) from exc
        catalog_owner_id = getattr(_token, "owner_id", None)
        if catalog_owner_id is not None:
            from zerg.services.console_turns import dispatch_catalog_claimed_turn

            for event in events:
                terminal_state = str((event.payload or {}).get("terminal_state") or "")
                if (
                    event.kind != "terminal_signal"
                    or event.run_id is None
                    or event.session_id is None
                    or event.thread_id is None
                    or event.device_id is None
                    or terminal_state
                    not in {
                        "run_completed",
                        "run_failed",
                        "run_cancelled",
                    }
                ):
                    continue
                outcome = {
                    "run_completed": "completed",
                    "run_cancelled": "cancelled",
                }.get(terminal_state, "failed")
                turn_result = await catalogd.call(
                    "session.console.turn.update.v2",
                    {
                        "turn": {
                            "run_id": str(event.run_id),
                            "owner_id": int(catalog_owner_id),
                            "session_id": str(event.session_id),
                            "thread_id": str(event.thread_id),
                            "provider": event.provider,
                            "device_id": event.device_id,
                            "state": outcome,
                            "error": None if outcome == "completed" else terminal_state,
                            "updated_at": (event.occurred_at or now_utc).isoformat(),
                        }
                    },
                )
                next_turn = turn_result.get("next_turn")
                if isinstance(next_turn, dict):
                    await dispatch_catalog_claimed_turn(
                        owner_id=int(catalog_owner_id),
                        turn=next_turn,
                        client=catalogd,
                    )
        commit_seq = raw_result.pop("commit_seq", None)
        if not isinstance(commit_seq, str) or not commit_seq.isdecimal():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_protocol_error", "message": "Catalog returned an invalid runtime result."},
            )
        try:
            result = RuntimeEventBatchResult.model_validate(raw_result)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_protocol_error", "message": "Catalog returned an invalid runtime result."},
            ) from exc
        response.headers["X-Catalog-Commit-Seq"] = commit_seq
        response.headers["X-Runtime-Label"] = "catalogd-runtime-state"
        _publish_runtime_updates(result, catalog_commit_seq=commit_seq)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ingest runtime observations",
        ) from exc


def _is_bridge_live_transcript_event(event) -> bool:
    # This was a codex-only copy of session_runtime._is_bridge_transcript_event,
    # which covers codex, cursor, and opencode. Cursor and OpenCode stream
    # batches therefore never took the live-transcript fast path: their previews
    # still landed via the DB overlay, but they lost instant SSE fanout and paid
    # the full notification/widget cost the fast path exists to skip. One
    # predicate, so a new streaming source cannot be recognized by one and not
    # the other.
    return _is_bridge_transcript_event(event)


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
    is_tool = payload.get("progress_kind") == "console_live_tool_item"
    command = str(payload.get("command") or "").strip()
    output = str(payload.get("output") or "")
    text = (output.strip() or command) if is_tool else str(payload.get("live_text") or "").strip()
    if not text or event.session_id is None:
        return None

    seq = _coerce_nonnegative_int(payload.get("seq"))
    observed_at = event.occurred_at or now
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    else:
        observed_at = observed_at.astimezone(timezone.utc)

    thread_id = str(payload.get("thread_id") or event.thread_id or "unknown-thread").strip() or "unknown-thread"
    turn_id = str(payload.get("turn_id") or "unknown-turn").strip() or "unknown-turn"
    cursor_seq = str(seq) if seq is not None else "unknown-seq"
    return {
        "event_id": seq or 0,
        "text": text,
        "role": "assistant",
        "tool_name": "exec" if is_tool else None,
        "tool_input_json": {"command": command} if is_tool else None,
        "tool_output_text": output if is_tool and output else None,
        "tool_call_id": str(payload.get("item_id") or "") or None,
        "tool_call_state": (
            "completed"
            if is_tool and (payload.get("completed") or str(payload.get("status") or "").lower() in {"completed", "failed", "cancelled"})
            else "running"
            if is_tool
            else None
        ),
        "event_origin": "live_provisional",
        "timestamp": observed_at.isoformat().replace("+00:00", "Z"),
        "is_provisional": True,
        "is_complete": bool(payload.get("turn_completed") or payload.get("completed")),
        "content_cursor": f"{event.source}:{event.session_id}:{thread_id}:{turn_id}:{cursor_seq}",
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
