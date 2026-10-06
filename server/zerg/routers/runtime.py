"""Runtime event ingest endpoints for Timeline runtime state."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timezone

import zstandard
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogRequestInvalid
from zerg.catalogd.client import CatalogRequestTooLarge
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.database import catalog_db_dependency
from zerg.database import live_store_configured
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.request_db import no_request_db
from zerg.metrics import event_age_at_ingest_seconds
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.session_live_previews import admit_preview_publication
from zerg.services.session_live_previews import live_preview_candidate_from_runtime_event
from zerg.services.session_live_previews import preview_payload_from_runtime_event
from zerg.services.session_runtime import RuntimeEventBatchIngest
from zerg.services.session_runtime import RuntimeEventBatchResult
from zerg.services.session_runtime import _is_bridge_transcript_event

# A batch is one catalogd apply, so it can never usefully exceed one catalogd
# frame (8 MiB); twice that bounds both what a body may carry on the wire and
# what a small zstd body may expand into.
_MAX_BATCH_BYTES = 16 * 1024 * 1024
_ZSTD_MAX_WINDOW_BYTES = 8 * 1024 * 1024
_ZSTD_READ_CHUNK_BYTES = 1024 * 1024


def _batch_too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={"code": "runtime_batch_too_large", "message": "Runtime batch exceeds one catalog apply."},
    )


def _decode_zstd_batch(body: bytes) -> bytes:
    decoded = bytearray()
    try:
        decompressor = zstandard.ZstdDecompressor(max_window_size=_ZSTD_MAX_WINDOW_BYTES)
        with decompressor.stream_reader(body, read_across_frames=True) as reader:
            while chunk := reader.read(_ZSTD_READ_CHUNK_BYTES):
                if len(decoded) + len(chunk) > _MAX_BATCH_BYTES:
                    raise _batch_too_large()
                decoded.extend(chunk)
    except zstandard.ZstdError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_content_encoding", "message": "Runtime batch is not valid zstd."},
        ) from exc
    return bytes(decoded)


class _RuntimeBatchRequest(Request):
    """A runtime batch body, decoded when the Machine Agent sent it as zstd.

    The Machine Agent encodes batches because its uplink, not this host,
    bounds how fast a backlog drains, and runtime events restate their keys,
    ids and preview text event after event. An older one sends them plain.
    """

    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            wire = bytearray()
            async for chunk in self.stream():
                if len(wire) + len(chunk) > _MAX_BATCH_BYTES:
                    raise _batch_too_large()
                wire.extend(chunk)
            body = bytes(wire)
            if (self.headers.get("content-encoding") or "").strip().lower() == "zstd":
                body = _decode_zstd_batch(body)
            self._body = body
        return self._body


class _RuntimeBatchRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def decoding_handler(request: Request) -> Response:
            return await handler(_RuntimeBatchRequest(request.scope, request.receive))

        return decoding_handler


router = APIRouter(prefix="/agents/runtime", tags=["agents"], route_class=_RuntimeBatchRoute)
_catalog_db_dependency = catalog_db_dependency()

_HOT_RUNTIME_QUEUE_TIMEOUT_SECONDS = 2.0


_settings = get_settings()
_runtime_db_dependency = (
    _catalog_db_dependency
    if _settings.testing or os.getenv("TESTING", "").strip().lower() in {"1", "true", "yes", "on"} or not live_store_configured()
    else no_request_db
)


def _canary_runtime_marker(event) -> tuple[int, int] | None:
    if (
        (event.provider or "").strip().lower() != "canary"
        or (event.source or "").strip().lower() != "canary_producer"
        or event.kind != "progress_signal"
        or event.session_id is None
        or event.runtime_key != f"canary:{event.session_id}"
    ):
        return None
    payload = event.payload or {}
    canary_seq = payload.get("canary_seq")
    emitted_at_ms = payload.get("canary_emitted_at_ms")
    if type(canary_seq) is not int or canary_seq < 0 or type(emitted_at_ms) is not int or emitted_at_ms <= 0:
        return None
    occurred_at = event.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    occurred_at_ms = int(occurred_at.timestamp() * 1000)
    if abs(occurred_at_ms - emitted_at_ms) > 5_000:
        return None
    return canary_seq, emitted_at_ms


@router.post("/events/batch", response_model=RuntimeEventBatchResult)
async def ingest_runtime_observation_batch(
    payload: RuntimeEventBatchIngest,
    response: Response,
    db: Session | None = Depends(_runtime_db_dependency),
    _token: object = Depends(verify_agents_caller),
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

        live_transcript_events = [event for event in events if _is_bridge_live_transcript_event(event)]
        if live_transcript_events:
            _publish_live_transcript_previews(live_transcript_events, now=now_utc)

        def _publish_runtime_updates(
            result: RuntimeEventBatchResult,
            *,
            catalog_commit_seq: str | int | None = None,
        ) -> None:
            updated_runtime_keys = set(result.updated_runtime_keys)
            if not updated_runtime_keys:
                return

            from zerg.services.session_pubsub import publish_session_runtime_update

            events_by_session: dict[str, list] = {}
            for event in events:
                if event.session_id is None or event.runtime_key not in updated_runtime_keys:
                    continue
                events_by_session.setdefault(str(event.session_id), []).append(event)
            for session_id, session_events in events_by_session.items():
                event = next(
                    (candidate for candidate in reversed(session_events) if _canary_runtime_marker(candidate) is not None),
                    session_events[0],
                )
                marker = _canary_runtime_marker(event)
                publish_session_runtime_update(
                    session_id=session_id,
                    provider=event.provider,
                    source=event.source,
                    catalog_commit_seq=catalog_commit_seq,
                    canary_seq=marker[0] if marker is not None else None,
                    canary_emitted_at_ms=marker[1] if marker is not None else None,
                )

        catalogd = get_catalogd_client()
        if catalogd is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            )
        catalog_events = _without_superseded_print_overlays(events)
        try:
            raw_result = await catalogd.call(
                "session.runtime.apply.v2",
                {"events": [event.model_dump(mode="json") for event in catalog_events]},
                timeout_seconds=_HOT_RUNTIME_QUEUE_TIMEOUT_SECONDS,
            )
        except CatalogRequestTooLarge as exc:
            # Permanent for this request, not for its events: the Machine Agent
            # resends them in smaller requests. A 503 here made it resend the
            # same oversized batch forever.
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={
                    "code": "runtime_batch_too_large",
                    "message": "Runtime batch exceeds one catalog apply; send fewer bytes per request.",
                },
            ) from exc
        except CatalogRequestInvalid as exc:
            # Any other request catalogd could not be sent is just as
            # deterministic: the same answer catalogd's own invalid_request gets
            # below, so the Machine Agent isolates the event that causes it
            # instead of resending the batch forever.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_runtime_batch", "message": str(exc)},
            ) from exc
        except CatalogUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "catalog_unavailable", "message": "Catalog mutation is temporarily unavailable."},
            ) from exc
        except CatalogRemoteError as exc:
            if getattr(exc, "code", None) == "invalid_request":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "invalid_runtime_batch", "message": str(exc)},
                ) from exc
            raise HTTPException(
                status_code=(status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_500_INTERNAL_SERVER_ERROR),
                detail={
                    "code": "catalog_unavailable" if exc.retryable else "catalog_operation_failed",
                    "message": ("Catalog mutation is temporarily unavailable." if exc.retryable else "Catalog runtime mutation failed."),
                },
            ) from exc
        # catalogd settled each Console turn whose adapter reported a run
        # terminal inside the runtime batch's own transaction; all that is
        # left here is dispatching the FIFO turns that transition claimed.
        console_next_turns = raw_result.pop("console_next_turns", None) or []
        if console_next_turns:
            from zerg.services.console_turns import dispatch_catalog_claimed_turn

            for claimed in console_next_turns:
                await dispatch_catalog_claimed_turn(
                    owner_id=int(claimed["owner_id"]),
                    turn=claimed["turn"],
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
        superseded = len(events) - len(catalog_events)
        if superseded:
            # Received and deliberately not applied: a newer overlay in this
            # batch carries everything they said.
            result.accepted += superseded
            result.ignored += superseded
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


_PRINT_STREAM_OVERLAYS = {("pi", "pi_print", "pi_print_stream"), ("omp", "omp_print", "omp_print_stream")}


def _print_overlay_key(event) -> tuple[tuple[str, str], int] | None:
    payload = event.payload or {}
    overlay = ((event.provider or "").strip().lower(), (event.source or "").strip().lower(), payload.get("progress_kind"))
    if event.kind != "progress_signal" or overlay not in _PRINT_STREAM_OVERLAYS:
        return None
    candidate = live_preview_candidate_from_runtime_event(event, observation_id=event.dedupe_key)
    if candidate is None or candidate.seq is None:
        return None
    return (str(candidate.session_id), candidate.turn_key), candidate.seq


def _without_superseded_print_overlays(events: list) -> list:
    """Drop only print-stream preview candidates replaced by newer candidates in the batch.

    The catalog applies these candidates as each message's live preview, keeping
    the highest ``seq``. A backlog of 128 of them for a 25 KB reply outran
    catalogd's 2 s budget, so the batch 503'd and was resent unchanged and the
    run's terminal event behind it never landed (OMP Console, 2026-10-04: the
    turn stayed running and its queued follow-up never started). The canonical
    preview builder supplies both candidate eligibility and per-item identity;
    lifecycle events pass through without displacing a preview.
    """
    overlays = [_print_overlay_key(event) for event in events]
    newest: dict[tuple[str, str], tuple[int, int]] = {}
    for index, overlay in enumerate(overlays):
        if overlay is None:
            continue
        key, seq = overlay
        if key not in newest or seq >= newest[key][0]:
            newest[key] = (seq, index)
    keep = {index for _seq, index in newest.values()}
    return [event for index, event in enumerate(events) if overlays[index] is None or index in keep]


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
    from zerg.services.session_pubsub import get_pubsub
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
    heads = get_pubsub().preview_heads
    for sid, (event, preview) in latest_by_session.items():
        # A batch the machine resent after a lost response, or one that lands
        # behind a newer batch, must not repeat or rewind what subscribers saw.
        # Identity is the projection's: the observation the machine minted plus
        # its ordering, never the text or the timestamp alone.
        candidate = live_preview_candidate_from_runtime_event(
            event,
            observation_id=f"live:{event.source}:{event.dedupe_key}",
        )
        if candidate is None:
            # A payload with no candidate has no identity the projection will
            # ever hold, so there is nothing a replay could be compared to.
            logger.warning("live_transcript preview has no durable identity session=%s dedupe_key=%s", sid, event.dedupe_key)
            continue
        if not admit_preview_publication(heads, candidate):
            logger.info(
                "live_transcript replay suppressed session=%s seq=%s dedupe_key=%s",
                sid,
                _preview_seq(preview),
                event.dedupe_key,
            )
            continue
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
    if (
        (event.provider or "").strip().lower() in {"pi", "omp"}
        and (event.source or "").strip().lower() in {"pi_print", "omp_print"}
        and (event.payload or {}).get("progress_kind") in {"pi_print_stream", "omp_print_stream"}
    ):
        return preview_payload_from_runtime_event(
            event,
            observation_id=f"runtime:{event.source}:{event.dedupe_key}",
        )
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
