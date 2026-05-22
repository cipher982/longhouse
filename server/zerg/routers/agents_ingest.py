"""Agents API — session ingest endpoint."""

import gzip
import io
import json
import logging
import time
from datetime import datetime
from datetime import timezone
from uuid import UUID

import zstandard
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.metrics import agents_ingest_decode_seconds
from zerg.metrics import agents_ingest_events_total
from zerg.metrics import agents_ingest_payload_bytes
from zerg.metrics import agents_ingest_requests_total
from zerg.metrics import agents_ingest_write_seconds
from zerg.metrics import event_age_at_ingest_seconds
from zerg.models.device_token import DeviceToken
from zerg.observability import get_tracer
from zerg.observability import set_span_attributes
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest
from zerg.services.session_views import IngestResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])
SHIP_TRACE_HEADER = "X-Longhouse-Ship-Trace"


def _unix_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ship_trace_from_request(request: Request) -> dict | None:
    raw = request.headers.get(SHIP_TRACE_HEADER)
    if not raw or len(raw) > 4096:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("schema") != "ship_trace.v1":
        return None
    return value


def _write_serializer_label_for_ship_trace(ship_trace: dict | None) -> str:
    work_context = ship_trace.get("work_context") if ship_trace else None
    if work_context == "live_transcript":
        return "ingest-live"
    if work_context == "spool_replay":
        return "ingest-replay"
    if work_context == "reconciliation_scan":
        return "ingest-scan"
    return "ingest"


def _ship_trace_id(ship_trace: dict | None) -> str | None:
    trace_id = str(ship_trace.get("trace_id") or "").strip() if ship_trace else ""
    return trace_id or None


# Phase 5: per-label commit chunk sizing. Live ingest stays conservative so
# health checks and SSE readers aren't starved between chunks; replay/scan
# can amortise the WAL fsync cost over much larger transactions.
_INGEST_CHUNK_BY_LABEL: dict[str, int] = {
    "ingest-live": 200,
    "ingest": 500,
    "ingest-replay": 1000,
    "ingest-scan": 1000,
}


def _ingest_chunk_for_label(label: str) -> int:
    return _INGEST_CHUNK_BY_LABEL.get(label, 200)


def _persist_ship_trace_event(
    db: Session,
    *,
    data: SessionIngest,
    result: IngestResponse,
    ship_trace: dict | None,
    server_trace: dict,
) -> None:
    if not ship_trace or result.events_inserted <= 0:
        return
    trace_id = str(ship_trace.get("trace_id") or "").strip()
    if not trace_id:
        return

    try:
        from zerg.services.session_runtime import RuntimeEventIngest
        from zerg.services.session_runtime import ingest_runtime_events
        from zerg.services.session_runtime import runtime_key_for_session

        session_id = UUID(str(result.session_id))
        payload = {
            "progress_kind": "ship_pipeline_trace",
            "ship_trace": ship_trace,
            "server_trace": server_trace,
        }
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key_for_session(data.provider, str(session_id)),
                    session_id=session_id,
                    provider=data.provider,
                    device_id=data.device_id,
                    source="agents_ingest_trace",
                    kind="binding_signal",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key=f"ship_trace:{session_id}:{trace_id}",
                    payload=payload,
                )
            ],
        )
    except Exception:
        logger.debug("Failed to persist ship trace event", exc_info=True)


def _record_server_fanout_observation(
    db: Session,
    *,
    session_id: UUID,
    provider: str,
    device_id: str | None,
    payload: dict,
    ship_trace: dict | None,
) -> None:
    try:
        from zerg.services.session_observations import OBS_KIND_SERVER_FANOUT
        from zerg.services.session_observations import SOURCE_DOMAIN_SERVER
        from zerg.services.session_observations import record_session_observation

        fanout_at_ms = payload.get("server_fanout_at_ms")
        fanout_at = (
            datetime.fromtimestamp(int(fanout_at_ms) / 1000.0, tz=timezone.utc)
            if isinstance(fanout_at_ms, int)
            else datetime.now(timezone.utc)
        )
        trace_id = _ship_trace_id(ship_trace)
        cursor = f"trace:{trace_id}" if trace_id else f"event:{payload.get('latest_event_id') or 'unknown'}"
        record_session_observation(
            db,
            observation_id=(
                f"server_fanout:{session_id}:"
                f"{trace_id or payload.get('latest_event_id') or payload.get('server_fanout_at_ms')}"
            ),
            session_id=session_id,
            runtime_key=None,
            provider=provider,
            device_id=device_id,
            source_domain=SOURCE_DOMAIN_SERVER,
            source="session_pubsub",
            kind=OBS_KIND_SERVER_FANOUT,
            observed_at=fanout_at,
            source_cursor=cursor,
            payload=payload,
        )
    except Exception:
        logger.warning("Failed to persist server fanout observation", exc_info=True)


async def _persist_server_fanout_observation(
    db: Session,
    *,
    session_id: UUID,
    provider: str,
    device_id: str | None,
    payload: dict,
    ship_trace: dict | None,
) -> None:
    try:
        from zerg.services.write_serializer import get_write_serializer

        ws = get_write_serializer()

        def _do(write_db: Session) -> None:
            _record_server_fanout_observation(
                write_db,
                session_id=session_id,
                provider=provider,
                device_id=device_id,
                payload=payload,
                ship_trace=ship_trace,
            )

        await ws.execute_or_direct(_do, db, label="server-fanout")
    except Exception:
        logger.warning("Failed to persist server fanout observation", exc_info=True)


# Hard cap on decompressed ingest bodies. Engine splits batches at
# `max_batch_bytes` (default 50 MiB *compressed*); a healthy decompressed
# JSONL batch decompresses to roughly 5-10× that. 256 MiB is comfortably
# above the largest legitimate batch and well below memory pressure on a
# Runtime Host. zstd bombs typically aim for 1000×+ ratios, so this caps
# the worst case at the same order of magnitude as legitimate traffic.
MAX_DECOMPRESSED_BODY_BYTES: int = 256 * 1024 * 1024


async def decompress_if_gzipped(request: Request) -> tuple[bytes, int, str]:
    """Decompress request body if gzip or zstd encoded.

    Returns:
        Tuple of (decompressed request body, wire bytes, content encoding)

    Raises 413 if the decompressed body would exceed
    [`MAX_DECOMPRESSED_BODY_BYTES`]. This is the zstd/gzip-bomb guard:
    upstream nginx caps the *compressed* request, but a tiny compressed
    body can decompress to many GiB if we don't bound the stream.

    Identity (uncompressed) bodies are also bounded by the same cap, so
    callers can't dodge the limit by simply not setting Content-Encoding.
    Unsupported encodings are rejected with 415.
    """
    body = await request.body()
    wire_bytes = len(body)
    content_encoding = request.headers.get("Content-Encoding", "").lower()

    if content_encoding == "gzip":
        try:
            body = _decompress_bounded_gzip(body)
        except (gzip.BadGzipFile, EOFError, OSError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid gzip content: {e}",
            )
    elif content_encoding == "zstd":
        try:
            body = _decompress_bounded_zstd(body)
        except zstandard.ZstdError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid zstd content: {e}",
            )
    elif content_encoding in ("", "identity"):
        if len(body) > MAX_DECOMPRESSED_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Identity body exceeds {MAX_DECOMPRESSED_BODY_BYTES} bytes"
                ),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported Content-Encoding: {content_encoding}",
        )

    return body, wire_bytes, content_encoding or "identity"


def _decompress_bounded_gzip(body: bytes) -> bytes:
    """Streaming gzip decompress with a hard size cap. 413 on overflow."""
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        while True:
            chunk = gz.read(1024 * 1024)
            if not chunk:
                break
            if len(out) + len(chunk) > MAX_DECOMPRESSED_BODY_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        f"Decompressed gzip body exceeds "
                        f"{MAX_DECOMPRESSED_BODY_BYTES} bytes"
                    ),
                )
            out.extend(chunk)
    return bytes(out)


def _decompress_bounded_zstd(body: bytes) -> bytes:
    """Streaming zstd decompress with a hard size cap. 413 on overflow."""
    out = bytearray()
    dctx = zstandard.ZstdDecompressor()
    with dctx.stream_reader(body) as reader:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            if len(out) + len(chunk) > MAX_DECOMPRESSED_BODY_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        f"Decompressed zstd body exceeds "
                        f"{MAX_DECOMPRESSED_BODY_BYTES} bytes"
                    ),
                )
            out.extend(chunk)
    return bytes(out)


@router.post("/ingest", response_model=IngestResponse)
async def ingest_session(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    auth_token: DeviceToken | ManagedLocalHookToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> IngestResponse:
    """Ingest a session with events.

    Creates or updates a session and inserts events, handling deduplication
    automatically via event hashing.

    This endpoint is called by the shipper to sync local session files
    (e.g., ~/.claude/projects/...) to Zerg.

    Features:
    - Accepts gzip-compressed payloads (Content-Encoding: gzip)
    - Triggers async background summary/embedding/turn-loop work after successful ingest
    """
    tracer = get_tracer(__name__)
    if isinstance(auth_token, ManagedLocalHookToken):
        auth_kind_label = "managed_local_hook"
    elif auth_token is not None:
        auth_kind_label = "device_token"
    else:
        auth_kind_label = "none"
    provider_label = "unknown"
    content_encoding_label = request.headers.get("Content-Encoding", "").lower() or "identity"
    request_status_label = "internal_error"
    handler_entered_at_ms = _unix_ms()
    decode_finished_at_ms: int | None = None
    validate_finished_at_ms: int | None = None
    ship_trace = _ship_trace_from_request(request)
    with tracer.start_as_current_span("longhouse.ingest") as span:
        set_span_attributes(
            span,
            {
                "http.route": "/api/agents/ingest",
                "longhouse.ingest.auth_kind": auth_kind_label,
            },
        )

        try:
            with tracer.start_as_current_span("longhouse.ingest.decode") as decode_span:
                decode_started = time.monotonic()
                body, wire_bytes, content_encoding = await decompress_if_gzipped(request)
                decode_ms = round((time.monotonic() - decode_started) * 1000, 1)
                decode_finished_at_ms = _unix_ms()
                content_encoding_label = content_encoding
                set_span_attributes(
                    decode_span,
                    {
                        "longhouse.ingest.content_encoding": content_encoding,
                        "longhouse.ingest.body_bytes_wire": wire_bytes,
                        "longhouse.ingest.body_bytes_decoded": len(body),
                        "longhouse.ingest.decode_ms": decode_ms,
                    },
                )
                agents_ingest_decode_seconds.labels(content_encoding=content_encoding_label).observe(decode_ms / 1000.0)
                agents_ingest_payload_bytes.labels(
                    content_encoding=content_encoding_label,
                    kind="wire",
                ).observe(wire_bytes)
                agents_ingest_payload_bytes.labels(
                    content_encoding=content_encoding_label,
                    kind="decoded",
                ).observe(len(body))

            with tracer.start_as_current_span("longhouse.ingest.validate") as validate_span:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as e:
                    request_status_label = "invalid_json"
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid JSON: {e}",
                    )

                try:
                    data = SessionIngest(**payload)
                except Exception as e:
                    request_status_label = "invalid_payload"
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Invalid payload: {e}",
                    )

                if isinstance(auth_token, ManagedLocalHookToken):
                    hook_session_id = UUID(auth_token.session_id)
                    if data.id is not None and data.id != hook_session_id:
                        request_status_label = "forbidden"
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Managed-local hook token does not match session",
                        )
                    data.id = hook_session_id
                    if auth_token.device_id:
                        data.device_id = auth_token.device_id
                elif auth_token:
                    if data.device_id and data.device_id != auth_token.device_id:
                        logger.debug(
                            "Device ID mismatch: payload %s != token %s, using token device_id",
                            data.device_id,
                            auth_token.device_id,
                        )
                    data.device_id = auth_token.device_id

                provider_label = data.provider or "unknown"
                validate_finished_at_ms = _unix_ms()
                set_span_attributes(
                    validate_span,
                    {
                        "longhouse.session.id": data.id,
                        "longhouse.provider": data.provider,
                        "longhouse.device.id": data.device_id,
                        "longhouse.ingest.event_count": len(data.events),
                    },
                )
                agents_ingest_events_total.labels(provider=provider_label, kind="received").inc(len(data.events))

                # Event age at ingest: emitted_at -> now. Hook token = managed local session.
                # Only provider-originated events (source_path set by engine) count toward
                # the SLA histogram. Server-synthesized events (live_session_dispatch) use
                # now() as timestamp and would deflate p50/p95 with ~0ms samples.
                managed_label = "true" if isinstance(auth_token, ManagedLocalHookToken) else "false"
                if data.events:
                    from datetime import datetime
                    from datetime import timezone

                    now_utc = datetime.now(timezone.utc)
                    for ev in data.events:
                        if ev.source_path is None:
                            continue
                        ev_ts = ev.timestamp
                        if ev_ts is None:
                            continue
                        if ev_ts.tzinfo is None:
                            ev_ts = ev_ts.replace(tzinfo=timezone.utc)
                        age_s = (now_utc - ev_ts).total_seconds()
                        # Clamp negative (clock skew) to 0; ignore ancient replays > 1h.
                        if age_s < 0:
                            age_s = 0.0
                        elif age_s > 3600:
                            continue
                        event_age_at_ingest_seconds.labels(
                            surface="ingest",
                            provider=provider_label,
                            managed=managed_label,
                        ).observe(age_s)
                set_span_attributes(
                    span,
                    {
                        "longhouse.session.id": data.id,
                        "longhouse.provider": data.provider,
                        "longhouse.device.id": data.device_id,
                        "longhouse.ingest.event_count": len(data.events),
                    },
                )

            from zerg.services.write_serializer import get_write_serializer

            ws = get_write_serializer()
            write_label = _write_serializer_label_for_ship_trace(ship_trace)

            ingest_chunk = _ingest_chunk_for_label(write_label)

            def _do_ingest(write_db):
                write_started_at_ms = _unix_ms()
                store = AgentsStore(write_db)
                result = store.ingest_session(data, chunk_size=ingest_chunk)
                store_returned_at_ms = _unix_ms()
                _persist_ship_trace_event(
                    write_db,
                    data=data,
                    result=result,
                    ship_trace=ship_trace,
                    server_trace={
                        "handler_entered_at_ms": handler_entered_at_ms,
                        "decode_finished_at_ms": decode_finished_at_ms,
                        "validate_finished_at_ms": validate_finished_at_ms,
                        "write_started_at_ms": write_started_at_ms,
                        "store_returned_at_ms": store_returned_at_ms,
                        "store_write_ms": store_returned_at_ms - write_started_at_ms,
                    },
                )
                return result

            with tracer.start_as_current_span("longhouse.ingest.write") as write_span:
                write_started = time.monotonic()
                result = await ws.execute_or_direct(_do_ingest, db, label=write_label)
                write_ms = round((time.monotonic() - write_started) * 1000, 1)
                agents_ingest_write_seconds.labels(provider=provider_label).observe(write_ms / 1000.0)

                # Phase 1: surface server-side queue/exec timing so the engine
                # can adapt concurrency in phase 2 without re-instrumenting.
                from zerg.services.write_serializer import last_write_timing

                timing = last_write_timing()
                if timing is not None:
                    response.headers["X-Ingest-Queue-Wait-Ms"] = f"{timing.queue_wait_ms:.1f}"
                    response.headers["X-Ingest-Exec-Ms"] = f"{timing.exec_ms:.1f}"
                    if timing.label:
                        response.headers["X-Ingest-Label"] = timing.label
                response.headers["X-Ingest-Commit-Count"] = str(result.commit_count)
                response.headers["X-Ingest-Commit-Ms"] = f"{result.commit_ms_total:.1f}"
                response.headers["X-Ingest-Chunk-Size"] = str(ingest_chunk)
                set_span_attributes(
                    write_span,
                    {
                        "longhouse.session.id": result.session_id,
                        "longhouse.ingest.events_inserted": result.events_inserted,
                        "longhouse.ingest.events_skipped": result.events_skipped,
                        "longhouse.ingest.session_created": result.session_created,
                        "longhouse.ingest.write_ms": write_ms,
                        "longhouse.ingest.write_label": write_label,
                        "longhouse.ingest.commit_count": result.commit_count,
                        "longhouse.ingest.commit_ms_total": result.commit_ms_total,
                        "longhouse.ingest.chunk_size": ingest_chunk,
                    },
                )
                agents_ingest_events_total.labels(provider=provider_label, kind="inserted").inc(result.events_inserted)
                agents_ingest_events_total.labels(provider=provider_label, kind="skipped").inc(result.events_skipped)

            # Publish to per-session pubsub so SSE subscribers wake directly.
            # Lives outside the DB transaction: publish only reflects persisted state.
            if result.events_inserted > 0:
                from zerg.services.session_pubsub import TOPIC_TIMELINE
                from zerg.services.session_pubsub import get_pubsub
                from zerg.services.session_pubsub import topic_session

                bus = get_pubsub()
                session_id_str = str(result.session_id)
                fanout_at_ms = _unix_ms()
                latest_event_id = result.latest_inserted_event_id
                trace_id = _ship_trace_id(ship_trace)
                payload = {
                    "kind": "ingest",
                    "session_id": session_id_str,
                    "events_inserted": result.events_inserted,
                    "provider": provider_label,
                    "latest_event_id": latest_event_id,
                    "server_fanout_at_ms": fanout_at_ms,
                    "ship_trace_id": trace_id,
                }
                session_pubsub_seq = bus.publish(topic_session(session_id_str), payload)
                timeline_pubsub_seq = bus.publish(TOPIC_TIMELINE, payload)
                await _persist_server_fanout_observation(
                    db,
                    session_id=result.session_id,
                    provider=provider_label,
                    device_id=data.device_id,
                    payload={
                        **payload,
                        "session_pubsub_seq": session_pubsub_seq,
                        "timeline_pubsub_seq": timeline_pubsub_seq,
                    },
                    ship_trace=ship_trace,
                )

            set_span_attributes(
                span,
                {
                    "longhouse.ingest.events_inserted": result.events_inserted,
                    "longhouse.ingest.events_skipped": result.events_skipped,
                    "longhouse.ingest.session_created": result.session_created,
                },
            )
            request_status_label = "ok"
            return IngestResponse(
                session_id=str(result.session_id),
                events_inserted=result.events_inserted,
                events_skipped=result.events_skipped,
                session_created=result.session_created,
            )

        except HTTPException as exc:
            if request_status_label == "internal_error":
                if exc.status_code == status.HTTP_400_BAD_REQUEST:
                    request_status_label = "bad_request"
                elif exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
                    request_status_label = "unprocessable_entity"
                else:
                    request_status_label = f"http_{exc.status_code}"
            raise
        except Exception:
            logger.exception("Failed to ingest session")
            request_status_label = "internal_error"
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to ingest session",
            )
        finally:
            agents_ingest_requests_total.labels(
                auth_kind=auth_kind_label,
                provider=provider_label,
                status=request_status_label,
            ).inc()
