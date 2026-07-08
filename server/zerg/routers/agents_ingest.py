"""Agents API — session ingest endpoint."""

import asyncio
import gzip
import io
import json
import logging
import os
import time
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

import zstandard
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.config import get_settings
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
from zerg.services.agents import AgentsStore
from zerg.services.agents import IngestResult
from zerg.services.agents import SessionIngest
from zerg.services.agents.store import is_workflow_journal_only_payload
from zerg.services.session_views import IngestResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])
SHIP_TRACE_HEADER = "X-Longhouse-Ship-Trace"
_TRUTHY_ENV = {"1", "true", "yes", "on"}


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
    # Missing or unknown trace context needs compatibility-grade session
    # counters, but it should still queue/admit like background archive work.
    return "ingest"


def _ship_trace_id(ship_trace: dict | None) -> str | None:
    trace_id = str(ship_trace.get("trace_id") or "").strip() if ship_trace else ""
    return trace_id or None


# Phase 5: per-label commit chunk sizing. Live ingest stays conservative so
# health checks and SSE readers aren't starved between chunks; replay/scan
# can amortise the WAL fsync cost over much larger transactions.
_INGEST_CHUNK_BY_LABEL: dict[str, int] = {
    "ingest-live": 200,
    "ingest": 100,
    # Archive repair can arrive as a large historical backlog after reboot or
    # deploy repair. Keep these chunks modest so replay cannot monopolize the
    # single SQLite writer long enough to starve health and launch requests.
    "ingest-replay": 100,
    "ingest-scan": 100,
}

_ARCHIVE_INGEST_LABELS = {"ingest", "ingest-replay", "ingest-scan"}
_COOPERATIVE_INGEST_LABELS = _ARCHIVE_INGEST_LABELS | {"ingest-live"}
_DEFER_DERIVED_PROJECTION_LABELS = {"ingest", "ingest-replay", "ingest-scan"}
_SYNC_SESSION_COUNT_LABELS = {"ingest-live"}
_INCREMENTAL_SESSION_COUNT_LABELS = {"ingest"}
_ARCHIVE_INGEST_BACKPRESSURE_DETAIL = "Archive ingest backlog is throttled; retry shortly"
_ARCHIVE_INGEST_BACKPRESSURE_KIND = "archive_ingest_backpressure"
_LIVE_INGEST_BACKPRESSURE_DETAIL = "Live ingest is throttled because the database writer is busy; retry shortly"
_LIVE_INGEST_BACKPRESSURE_KIND = "live_ingest_backpressure"
_ARCHIVE_INGEST_MIN_RETRY_AFTER_SECONDS = 5
_ARCHIVE_INGEST_MAX_RETRY_AFTER_SECONDS = 60
_ARCHIVE_INGEST_ACTIVE_WRITER_RETRY_AFTER_SECONDS = 15
_ARCHIVE_INGEST_MAX_IN_FLIGHT = 4
_ARCHIVE_INGEST_WRITER_QUEUE_HARD_LIMIT = 50
_ARCHIVE_INGEST_ACTIVE_WRITER_GRACE_MS = 1000.0
_LIVE_INGEST_WRITER_QUEUE_HARD_LIMIT = 10
_LIVE_INGEST_ACTIVE_WRITER_GRACE_MS = 5_000.0
_ARCHIVE_INGEST_SLOTS = asyncio.Semaphore(_ARCHIVE_INGEST_MAX_IN_FLIGHT)
_ARCHIVE_INGEST_SUB_BATCH_MAX_ITEMS = 64
_INGEST_STAGE_HEADER_LIMIT = 8
_UNTRACED_INGEST_MAX_EVENTS = 200
_UNTRACED_INGEST_MAX_SOURCE_LINES = 200
_UNTRACED_INGEST_MAX_DECODED_BYTES = 2 * 1024 * 1024
_ARCHIVE_SHADOW_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_ARCHIVE_SHADOW_SESSION_LOCKS_GUARD = asyncio.Lock()


def _ingest_chunk_for_label(label: str) -> int:
    return _INGEST_CHUNK_BY_LABEL.get(label, 200)


def _ingest_lane_for_label(label: str) -> str:
    if label == "ingest-live":
        return "live"
    if label in _ARCHIVE_INGEST_LABELS:
        return "archive"
    return "default"


def _copy_session_ingest(
    data: SessionIngest,
    *,
    events: list,
    source_lines: list,
    rewind_hints: list,
) -> SessionIngest:
    update = {
        "events": events,
        "source_lines": source_lines,
        "rewind_hints": rewind_hints,
    }
    if hasattr(data, "model_copy"):
        return data.model_copy(update=update)
    return data.copy(update=update)


def _archive_ingest_batches(data: SessionIngest, *, max_items: int = _ARCHIVE_INGEST_SUB_BATCH_MAX_ITEMS) -> list[SessionIngest]:
    """Split ingest into serializer-sized cooperative units."""
    max_items = max(1, max_items)
    events = list(data.events)
    source_lines = list(data.source_lines or [])
    rewind_hints = list(data.rewind_hints or [])
    total = max(len(events), len(source_lines), 1)
    batches: list[SessionIngest] = []
    for start in range(0, total, max_items):
        end = start + max_items
        batches.append(
            _copy_session_ingest(
                data,
                events=events[start:end],
                source_lines=source_lines[start:end],
                # Rewind hints establish branch state; replay them only on the
                # first sub-batch so later chunks do not repeatedly signal rewind.
                rewind_hints=rewind_hints if start == 0 else [],
            )
        )
    return batches


async def _check_ingest_writer_pressure(write_label: str, response: Response) -> None:
    if write_label in _ARCHIVE_INGEST_LABELS:
        await _check_archive_ingest_writer_pressure(write_label, response)
    elif write_label == "ingest-live":
        await _check_live_ingest_writer_pressure(write_label, response)


def _merge_ingest_results(results: list[IngestResult]) -> IngestResult:
    if not results:
        raise ValueError("cannot merge empty ingest result set")
    first = results[0]
    latest_inserted_event_id = None
    for result in results:
        if result.latest_inserted_event_id is not None:
            latest_inserted_event_id = max(latest_inserted_event_id or 0, result.latest_inserted_event_id)
    store_stage_ms: dict[str, float] = {}
    for result in results:
        for label, value in result.store_stage_ms.items():
            store_stage_ms[label] = round(store_stage_ms.get(label, 0.0) + float(value), 3)
    return IngestResult(
        session_id=first.session_id,
        events_inserted=sum(result.events_inserted for result in results),
        events_skipped=sum(result.events_skipped for result in results),
        latest_inserted_event_id=latest_inserted_event_id,
        session_created=any(result.session_created for result in results),
        commit_count=sum(result.commit_count for result in results),
        commit_ms_total=sum(result.commit_ms_total for result in results),
        source_lines_inserted=sum(result.source_lines_inserted for result in results),
        store_stage_ms=store_stage_ms,
    )


def _merge_archive_primary_states(states: list[str]) -> str:
    """Return a compact response state for per-batch archive-primary writes."""

    if not states:
        return "disabled"
    if any(state == "fallback" for state in states):
        return "fallback"
    if any(state == "written" for state in states):
        return "written"
    if any(state == "prepared" for state in states):
        return "prepared"
    return "disabled"


def _sync_session_counts_for_label(label: str) -> bool:
    return label in _SYNC_SESSION_COUNT_LABELS


def _sync_derived_projections_for_label(label: str) -> bool:
    return label not in _DEFER_DERIVED_PROJECTION_LABELS


def _incremental_session_counts_for_label(label: str) -> bool:
    return label in _INCREMENTAL_SESSION_COUNT_LABELS


def _stage_timing_header_value(stage_ms: dict[str, float]) -> str:
    """Compact, bounded store-stage timing header for engine feedback."""
    cleaned: dict[str, float] = {}
    for name, value in stage_ms.items():
        if not name or not isinstance(value, int | float):
            continue
        value_f = float(value)
        if value_f < 0:
            continue
        cleaned[name] = round(value_f, 1)

    ordered = dict(
        sorted(
            cleaned.items(),
            key=lambda item: (item[0] != "total", -item[1], item[0]),
        )[:_INGEST_STAGE_HEADER_LIMIT]
    )
    return json.dumps(ordered, separators=(",", ":"), sort_keys=True)


def _archive_retry_after_for_queue_depth(queue_depth: int) -> int:
    return max(
        _ARCHIVE_INGEST_MIN_RETRY_AFTER_SECONDS,
        min(_ARCHIVE_INGEST_MAX_RETRY_AFTER_SECONDS, queue_depth * 2),
    )


def _archive_backpressure_headers(
    *,
    admission_state: str = "archive_slots_full",
    retry_after_seconds: int = _ARCHIVE_INGEST_MIN_RETRY_AFTER_SECONDS,
) -> dict[str, str]:
    return {
        "Retry-After": str(retry_after_seconds),
        "X-Ingest-Lane": "archive",
        "X-Ingest-Admission-State": admission_state,
        "X-Ingest-Backpressure": _ARCHIVE_INGEST_BACKPRESSURE_KIND,
        "X-Ingest-Error-Kind": _ARCHIVE_INGEST_BACKPRESSURE_KIND,
        "X-Ingest-Queue-Wait-Ms": "0.0",
        "X-Ingest-Exec-Ms": "0.0",
    }


def _live_backpressure_headers(
    *,
    admission_state: str,
    retry_after_seconds: int = _ARCHIVE_INGEST_MIN_RETRY_AFTER_SECONDS,
) -> dict[str, str]:
    return {
        "Retry-After": str(retry_after_seconds),
        "X-Ingest-Lane": "live",
        "X-Ingest-Admission-State": admission_state,
        "X-Ingest-Backpressure": _LIVE_INGEST_BACKPRESSURE_KIND,
        "X-Ingest-Error-Kind": _LIVE_INGEST_BACKPRESSURE_KIND,
        "X-Ingest-Queue-Wait-Ms": "0.0",
        "X-Ingest-Exec-Ms": "0.0",
    }


def _raise_archive_ingest_backpressure(
    response: Response,
    *,
    admission_state: str = "archive_slots_full",
    retry_after_seconds: int = _ARCHIVE_INGEST_MIN_RETRY_AFTER_SECONDS,
) -> None:
    headers = {
        **_archive_backpressure_headers(
            admission_state=admission_state,
            retry_after_seconds=retry_after_seconds,
        ),
        **dict(response.headers),
    }
    response.headers.update(headers)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_ARCHIVE_INGEST_BACKPRESSURE_DETAIL,
        headers=headers,
    )


def _raise_live_ingest_backpressure(
    response: Response,
    *,
    admission_state: str,
    retry_after_seconds: int = _ARCHIVE_INGEST_MIN_RETRY_AFTER_SECONDS,
) -> None:
    headers = {
        **_live_backpressure_headers(
            admission_state=admission_state,
            retry_after_seconds=retry_after_seconds,
        ),
        **dict(response.headers),
    }
    response.headers.update(headers)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_LIVE_INGEST_BACKPRESSURE_DETAIL,
        headers=headers,
    )


async def _check_archive_ingest_writer_pressure(write_label: str, response: Response) -> None:
    if write_label not in _ARCHIVE_INGEST_LABELS:
        return

    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()
    if ws.is_configured:
        repair_idle_queue = getattr(ws, "repair_idle_queue", None)
        if callable(repair_idle_queue):
            await repair_idle_queue()
        queue_depth = int(getattr(ws, "queue_depth", 0) or 0)
        writer_active = bool(getattr(ws, "writer_active", False))
        active_label = str(getattr(ws, "active_label", "") or "")
        active_age_ms = float(getattr(ws, "active_age_ms", 0.0) or 0.0)
        active_writer_is_stale = active_age_ms >= _ARCHIVE_INGEST_ACTIVE_WRITER_GRACE_MS
        active_label_is_archive = active_label in _ARCHIVE_INGEST_LABELS
        if queue_depth > 0:
            response.headers["X-Ingest-Writer-Queue-Depth"] = str(queue_depth)
        if queue_depth >= _ARCHIVE_INGEST_WRITER_QUEUE_HARD_LIMIT:
            _raise_archive_ingest_backpressure(
                response,
                admission_state="writer_queue_pressure",
                retry_after_seconds=_archive_retry_after_for_queue_depth(queue_depth),
            )
        if writer_active and active_label_is_archive:
            response.headers["X-Ingest-Writer-Active-Label"] = active_label
            response.headers["X-Ingest-Writer-Active-Age-Ms"] = f"{active_age_ms:.1f}"
            _raise_archive_ingest_backpressure(
                response,
                admission_state="archive_writer_busy",
                retry_after_seconds=_ARCHIVE_INGEST_ACTIVE_WRITER_RETRY_AFTER_SECONDS,
            )
        active_non_archive_writer_is_stale = writer_active and not active_label_is_archive and active_writer_is_stale
        if active_non_archive_writer_is_stale:
            response.headers["X-Ingest-Writer-Active-Label"] = active_label
            response.headers["X-Ingest-Writer-Active-Age-Ms"] = f"{active_age_ms:.1f}"
            _raise_archive_ingest_backpressure(response, admission_state="writer_pressure")


async def _check_live_ingest_writer_pressure(write_label: str, response: Response) -> None:
    if write_label != "ingest-live":
        return

    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()
    if not ws.is_configured:
        return
    repair_idle_queue = getattr(ws, "repair_idle_queue", None)
    if callable(repair_idle_queue):
        await repair_idle_queue()
    queue_depth = int(getattr(ws, "queue_depth", 0) or 0)
    writer_active = bool(getattr(ws, "writer_active", False))
    active_label = str(getattr(ws, "active_label", "") or "")
    active_age_ms = float(getattr(ws, "active_age_ms", 0.0) or 0.0)
    if queue_depth > 0:
        response.headers["X-Ingest-Writer-Queue-Depth"] = str(queue_depth)
    if queue_depth >= _LIVE_INGEST_WRITER_QUEUE_HARD_LIMIT:
        _raise_live_ingest_backpressure(
            response,
            admission_state="writer_queue_pressure",
            retry_after_seconds=_archive_retry_after_for_queue_depth(queue_depth),
        )
    if writer_active and active_age_ms >= _LIVE_INGEST_ACTIVE_WRITER_GRACE_MS:
        response.headers["X-Ingest-Writer-Active-Label"] = active_label
        response.headers["X-Ingest-Writer-Active-Age-Ms"] = f"{active_age_ms:.1f}"
        admission_state = "live_writer_busy" if active_label == "ingest-live" else "writer_pressure"
        _raise_live_ingest_backpressure(response, admission_state=admission_state)


async def _acquire_archive_ingest_slot(write_label: str, response: Response) -> bool:
    """Admit bounded background archive ingest into heavy request work.

    Archive replay/scan batches are reconstructable from local provider files.
    When a backlog wakes after deploy or repair, cap concurrent body
    decode/validation work, then let WriteSerializer's priority queue keep
    live transcript and runtime writes ahead of archive repair.
    """
    if write_label not in _ARCHIVE_INGEST_LABELS:
        return False

    await _check_archive_ingest_writer_pressure(write_label, response)

    if _ARCHIVE_INGEST_SLOTS.locked():
        _raise_archive_ingest_backpressure(response)

    await _ARCHIVE_INGEST_SLOTS.acquire()
    return True


def _release_archive_ingest_slot(acquired: bool) -> None:
    if acquired:
        _ARCHIVE_INGEST_SLOTS.release()


def _untraced_ingest_is_too_large(data: SessionIngest, decoded_bytes: int) -> bool:
    return (
        decoded_bytes > _UNTRACED_INGEST_MAX_DECODED_BYTES
        or len(data.events) > _UNTRACED_INGEST_MAX_EVENTS
        or len(data.source_lines or []) > _UNTRACED_INGEST_MAX_SOURCE_LINES
    )


def _raise_untraced_ingest_backpressure(response: Response) -> None:
    headers = _archive_backpressure_headers(
        admission_state="untraced_ingest_too_large",
        retry_after_seconds=_ARCHIVE_INGEST_MIN_RETRY_AFTER_SECONDS,
    )
    response.headers.update(headers)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Untraced archive ingest backlog is throttled; retry after traced live writes drain",
        headers=headers,
    )


def _json_timestamp(value: datetime) -> str:
    ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_live_ingest_transcript_preview(data: SessionIngest, ship_trace: dict | None) -> dict | None:
    """Return a pre-commit live preview for the newest assistant text event."""
    if not ship_trace or ship_trace.get("work_context") != "live_transcript":
        return None
    if data.id is None or not data.events:
        return None

    event = data.events[-1]
    if event.role != "assistant" or event.tool_name:
        return None
    text = str(event.content_text or "").strip()
    if not text:
        return None

    raw_event_id = event.source_offset or ship_trace.get("new_offset") or _unix_ms()
    try:
        event_id = int(raw_event_id)
    except (TypeError, ValueError):
        event_id = _unix_ms()

    trace_id = _ship_trace_id(ship_trace)
    cursor = f"ingest-live:{trace_id or data.id}:{event_id}"
    return {
        "event_id": event_id,
        "text": text,
        "event_origin": "live_provisional",
        "timestamp": _json_timestamp(event.timestamp),
        "is_provisional": True,
        "is_complete": True,
        "content_cursor": cursor,
        "is_stale": False,
        "stale_reason": None,
    }


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
        fanout_key = trace_id or payload.get("latest_event_id") or payload.get("server_fanout_at_ms")
        record_session_observation(
            db,
            observation_id=f"server_fanout:{session_id}:{fanout_key}",
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
    db: Session | None,
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

        if db is None:
            await ws.execute(_do, label="server-fanout")
        else:
            await ws.execute_or_direct(_do, db, label="server-fanout")
    except Exception:
        logger.warning("Failed to persist server fanout observation", exc_info=True)


def _is_testing_env() -> bool:
    return os.getenv("TESTING", "").strip().lower() in _TRUTHY_ENV


def _background_server_fanout_observation(
    *,
    session_id: UUID,
    provider: str,
    device_id: str | None,
    payload: dict,
    ship_trace: dict | None,
) -> None:
    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()
    if not ws.is_configured or _is_testing_env():
        return
    asyncio.create_task(
        _persist_server_fanout_observation(
            None,
            session_id=session_id,
            provider=provider,
            device_id=device_id,
            payload=payload,
            ship_trace=ship_trace,
        )
    )


async def _archive_shadow_session_lock(session_id: UUID | str) -> asyncio.Lock:
    async with _ARCHIVE_SHADOW_SESSION_LOCKS_GUARD:
        return _ARCHIVE_SHADOW_SESSION_LOCKS.setdefault(str(session_id), asyncio.Lock())


async def _write_shadow_archive_after_ingest(
    *,
    data: SessionIngest,
    result,
    fallback_db: Session,
) -> None:
    """Best-effort archive shadow write without holding the ingest writer slot."""
    from zerg.services.archive_shadow import insert_archive_chunk_manifests
    from zerg.services.archive_shadow import prepare_ingest_shadow_archive
    from zerg.services.write_serializer import get_write_serializer

    lock = await _archive_shadow_session_lock(result.session_id)
    try:
        async with lock:
            await _write_shadow_archive_after_ingest_locked(
                data=data,
                result=result,
                fallback_db=fallback_db,
                prepare_ingest_shadow_archive=prepare_ingest_shadow_archive,
                insert_archive_chunk_manifests=insert_archive_chunk_manifests,
                get_write_serializer=get_write_serializer,
            )
    except Exception:
        logger.warning("Shadow archive write failed after ingest for session %s", result.session_id, exc_info=True)


async def _write_shadow_archive_after_ingest_locked(
    *,
    data: SessionIngest,
    result,
    fallback_db: Session,
    prepare_ingest_shadow_archive,
    insert_archive_chunk_manifests,
    get_write_serializer,
) -> None:
    if _is_testing_env():
        prepared = prepare_ingest_shadow_archive(data=data, result=result, manifest_db=fallback_db)
    else:
        prepared = await asyncio.to_thread(
            _prepare_shadow_archive_with_fresh_manifest_db,
            data=data,
            result=result,
            prepare_ingest_shadow_archive=prepare_ingest_shadow_archive,
        )
    if not prepared.enabled or prepared.error or not prepared.chunks:
        return

    def _insert_manifests(write_db: Session) -> None:
        insert_archive_chunk_manifests(write_db, prepared.chunks)

    ws = get_write_serializer()
    if ws.is_configured and not _is_testing_env():
        await ws.execute(_insert_manifests, label="archive-shadow-manifest")
    else:
        await ws.execute_or_direct(_insert_manifests, fallback_db, label="archive-shadow-manifest")


def _prepare_shadow_archive_with_fresh_manifest_db(
    *,
    data: SessionIngest,
    result,
    prepare_ingest_shadow_archive,
    settings=None,
    force_enabled: bool = False,
):
    from zerg.database import get_session_factory

    SessionLocal = get_session_factory()
    with SessionLocal() as read_db:
        return prepare_ingest_shadow_archive(
            data=data,
            result=result,
            settings=settings,
            manifest_db=read_db,
            force_enabled=force_enabled,
        )


async def _prepare_archive_primary_before_ingest(
    *,
    data: SessionIngest,
    fallback_db: Session,
    settings,
):
    """Prepare archive-primary chunks before legacy raw writes run."""

    from zerg.services.archive_shadow import prepare_ingest_shadow_archive

    if data.id is None:
        raise ValueError("archive-primary ingest requires a resolved session id")
    result = IngestResult(
        session_id=data.id,
        events_inserted=0,
        events_skipped=0,
        session_created=False,
        source_lines_inserted=0,
    )
    if _is_testing_env():
        return prepare_ingest_shadow_archive(
            data=data,
            result=result,
            settings=settings,
            manifest_db=fallback_db,
            force_enabled=True,
        )
    return await asyncio.to_thread(
        _prepare_shadow_archive_with_fresh_manifest_db,
        data=data,
        result=result,
        prepare_ingest_shadow_archive=prepare_ingest_shadow_archive,
        settings=settings,
        force_enabled=True,
    )


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
    content_encoding = request.headers.get("Content-Encoding", "").lower()
    return await asyncio.to_thread(_decode_body_bytes, body, content_encoding)


def _decode_body_bytes(body: bytes, content_encoding: str) -> tuple[bytes, int, str]:
    wire_bytes = len(body)

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
                detail=(f"Identity body exceeds {MAX_DECOMPRESSED_BODY_BYTES} bytes"),
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
                    detail=(f"Decompressed gzip body exceeds {MAX_DECOMPRESSED_BODY_BYTES} bytes"),
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
                    detail=(f"Decompressed zstd body exceeds {MAX_DECOMPRESSED_BODY_BYTES} bytes"),
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
    write_label = _write_serializer_label_for_ship_trace(ship_trace)
    archive_slot_acquired = False
    with tracer.start_as_current_span("longhouse.ingest") as span:
        set_span_attributes(
            span,
            {
                "http.route": "/api/agents/ingest",
                "longhouse.ingest.auth_kind": auth_kind_label,
                "longhouse.ingest.write_label": write_label,
            },
        )

        try:
            archive_slot_acquired = await _acquire_archive_ingest_slot(write_label, response)
            await _check_live_ingest_writer_pressure(write_label, response)

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
                    payload = await asyncio.to_thread(json.loads, body)
                except json.JSONDecodeError as e:
                    request_status_label = "invalid_json"
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid JSON: {e}",
                    )

                try:
                    data = await asyncio.to_thread(lambda: SessionIngest(**payload))
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

                # Dynamic-workflow `journal.jsonl` is a control ledger, not a
                # session. Short-circuit BEFORE any archive prepare/write so it
                # never leaves archive chunk residue or an empty session. The
                # engine sees a 2xx and advances its offset, so it does not
                # re-ship. (The store guard is the in-process defense in depth.)
                if is_workflow_journal_only_payload(data):
                    request_status_label = "ok"
                    return IngestResponse(
                        session_id=str(data.id) if data.id else str(uuid4()),
                        events_inserted=0,
                        events_skipped=0,
                        session_created=False,
                    )

                if write_label == "ingest" and _untraced_ingest_is_too_large(data, len(body)):
                    request_status_label = "archive_backpressure"
                    _raise_untraced_ingest_backpressure(response)

                settings = get_settings()
                if settings.archive_primary_write_enabled and data.id is None:
                    data.id = uuid4()
                if not settings.archive_primary_write_enabled and not settings.legacy_raw_write_enabled:
                    request_status_label = "invalid_archive_config"
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Legacy raw writes cannot be disabled unless archive-primary writes are enabled",
                    )

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

                transcript_preview = _build_live_ingest_transcript_preview(data, ship_trace)
                if transcript_preview is not None:
                    from zerg.services.session_pubsub import publish_session_transcript_preview_update

                    publish_session_transcript_preview_update(
                        session_id=str(data.id),
                        provider=data.provider,
                        source="agents_ingest_live",
                        transcript_preview=transcript_preview,
                    )

            from zerg.services.write_serializer import get_write_serializer

            ws = get_write_serializer()
            ingest_chunk = _ingest_chunk_for_label(write_label)

            def _do_ingest(
                write_db,
                ingest_data: SessionIngest = data,
                batch_index: int = 0,
                archive_primary_prepared=None,
                archive_primary_state: str = "disabled",
                archive_primary_records_written: int = 0,
                legacy_raw_effective: bool = settings.legacy_raw_write_enabled,
            ) -> tuple[IngestResult, str, bool]:
                if write_label in _ARCHIVE_INGEST_LABELS:
                    set_active_stage = getattr(ws, "set_active_stage", None)
                    if callable(set_active_stage):
                        set_active_stage(f"{write_label}:batch-{batch_index + 1}:store-ingest")
                write_started_at_ms = _unix_ms()
                if archive_primary_prepared is not None and archive_primary_state == "prepared" and archive_primary_prepared.chunks:
                    from zerg.services.archive_shadow import insert_archive_chunk_manifests

                    try:
                        insert_archive_chunk_manifests(write_db, archive_primary_prepared.chunks)
                        archive_primary_state = "written"
                    except Exception:
                        if not settings.legacy_raw_write_enabled:
                            # Archive is the only raw store and the manifest write
                            # failed: fail closed with the same 503 semantics as a
                            # prepare failure, not a generic 500.
                            logger.warning(
                                "Archive-primary manifest insert failed for session %s " "and legacy raw fallback is disabled",
                                data.id,
                                exc_info=True,
                            )
                            raise HTTPException(
                                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Archive-primary write failed and legacy raw fallback is disabled",
                                headers={"X-Ingest-Archive-Primary": "failed"},
                            )
                        archive_primary_state = "fallback"
                        legacy_raw_effective = True
                        logger.warning(
                            "Archive-primary manifest insert failed for session %s; falling back to legacy raw writes",
                            data.id,
                            exc_info=True,
                        )
                elif archive_primary_state == "prepared":
                    archive_primary_state = "written"

                store = AgentsStore(write_db)
                result = store.ingest_session(
                    ingest_data,
                    chunk_size=ingest_chunk,
                    synchronous_projections=_sync_derived_projections_for_label(write_label),
                    synchronous_session_counts=_sync_session_counts_for_label(write_label),
                    incremental_session_counts=_incremental_session_counts_for_label(write_label),
                    write_legacy_raw=legacy_raw_effective,
                    raw_source_archived=archive_primary_state == "written" and archive_primary_records_written > 0,
                )
                store_returned_at_ms = _unix_ms()
                _persist_ship_trace_event(
                    write_db,
                    data=ingest_data,
                    result=result,
                    ship_trace=ship_trace,
                    server_trace={
                        "handler_entered_at_ms": handler_entered_at_ms,
                        "decode_finished_at_ms": decode_finished_at_ms,
                        "validate_finished_at_ms": validate_finished_at_ms,
                        "write_started_at_ms": write_started_at_ms,
                        "store_returned_at_ms": store_returned_at_ms,
                        "store_write_ms": store_returned_at_ms - write_started_at_ms,
                        "store_stage_ms": result.store_stage_ms,
                        "store_counts": {
                            "events_inserted": result.events_inserted,
                            "events_skipped": result.events_skipped,
                            "source_lines_inserted": result.source_lines_inserted,
                            "commit_count": result.commit_count,
                            "commit_ms_total": result.commit_ms_total,
                        },
                    },
                )
                return result, archive_primary_state, legacy_raw_effective

            with tracer.start_as_current_span("longhouse.ingest.write") as write_span:
                write_started = time.monotonic()
                ingest_batches = (
                    _archive_ingest_batches(data, max_items=min(ingest_chunk, _ARCHIVE_INGEST_SUB_BATCH_MAX_ITEMS))
                    if write_label in _COOPERATIVE_INGEST_LABELS
                    else [data]
                )
                write_results: list[IngestResult] = []
                archive_primary_states: list[str] = []
                legacy_raw_states: list[bool] = []
                for batch_index, ingest_batch in enumerate(ingest_batches):
                    if batch_index > 0 and write_label in _COOPERATIVE_INGEST_LABELS:
                        await asyncio.sleep(0)
                        await _check_ingest_writer_pressure(write_label, response)

                    archive_primary_prepared = None
                    archive_primary_state = "disabled"
                    archive_primary_records_written = 0
                    legacy_raw_effective = settings.legacy_raw_write_enabled
                    if settings.archive_primary_write_enabled:
                        archive_primary_prepared = await _prepare_archive_primary_before_ingest(
                            data=ingest_batch,
                            fallback_db=db,
                            settings=settings,
                        )
                        if archive_primary_prepared.error:
                            if not settings.legacy_raw_write_enabled:
                                request_status_label = "archive_primary_failed"
                                raise HTTPException(
                                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                    detail="Archive-primary write failed and legacy raw fallback is disabled",
                                    headers={"X-Ingest-Archive-Primary": "failed"},
                                )
                            archive_primary_state = "fallback"
                            legacy_raw_effective = True
                            logger.warning(
                                "Archive-primary prepare failed for session %s batch=%s; falling back to legacy raw writes: %s",
                                data.id,
                                batch_index + 1,
                                archive_primary_prepared.error,
                            )
                        else:
                            archive_primary_state = "prepared"
                            archive_primary_records_written = archive_primary_prepared.records_written

                    batch_result, batch_archive_primary_state, batch_legacy_raw_effective = await ws.execute_after_closing_request_session(
                        lambda write_db,
                        ingest_batch=ingest_batch,
                        batch_index=batch_index,
                        archive_primary_prepared=archive_primary_prepared,
                        archive_primary_state=archive_primary_state,
                        archive_primary_records_written=archive_primary_records_written,
                        legacy_raw_effective=legacy_raw_effective: _do_ingest(
                            write_db,
                            ingest_batch,
                            batch_index,
                            archive_primary_prepared,
                            archive_primary_state,
                            archive_primary_records_written,
                            legacy_raw_effective,
                        ),
                        db,
                        label=write_label,
                    )
                    write_results.append(batch_result)
                    archive_primary_states.append(batch_archive_primary_state)
                    legacy_raw_states.append(batch_legacy_raw_effective)
                result = _merge_ingest_results(write_results)
                archive_primary_state = _merge_archive_primary_states(archive_primary_states)
                legacy_raw_effective = any(legacy_raw_states) if legacy_raw_states else settings.legacy_raw_write_enabled
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
                response.headers["X-Ingest-Lane"] = _ingest_lane_for_label(write_label)
                admission_state = "archive_slot_acquired" if archive_slot_acquired else "not_applicable"
                response.headers["X-Ingest-Admission-State"] = admission_state
                response.headers["X-Ingest-Commit-Count"] = str(result.commit_count)
                response.headers["X-Ingest-Commit-Ms"] = f"{result.commit_ms_total:.1f}"
                response.headers["X-Ingest-Chunk-Size"] = str(ingest_chunk)
                response.headers["X-Ingest-Sub-Batches"] = str(len(ingest_batches))
                response.headers["X-Ingest-Archive-Primary"] = archive_primary_state
                response.headers["X-Ingest-Legacy-Raw"] = "enabled" if legacy_raw_effective else "disabled"
                response.headers["X-Ingest-Store-Stage-Ms"] = _stage_timing_header_value(result.store_stage_ms)
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

            if not settings.archive_primary_write_enabled:
                await _write_shadow_archive_after_ingest(data=data, result=result, fallback_db=db)

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
                fanout_payload = {
                    **payload,
                    "session_pubsub_seq": session_pubsub_seq,
                    "timeline_pubsub_seq": timeline_pubsub_seq,
                }
                _background_server_fanout_observation(
                    session_id=result.session_id,
                    provider=provider_label,
                    device_id=data.device_id,
                    payload=fanout_payload,
                    ship_trace=ship_trace,
                )
                if _is_testing_env():
                    await _persist_server_fanout_observation(
                        db,
                        session_id=result.session_id,
                        provider=provider_label,
                        device_id=data.device_id,
                        payload=fanout_payload,
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
            _release_archive_ingest_slot(archive_slot_acquired)
            agents_ingest_requests_total.labels(
                auth_kind=auth_kind_label,
                provider=provider_label,
                status=request_status_label,
            ).inc()
