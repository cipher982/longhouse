"""Agents API — session ingest endpoint."""

import asyncio
import gzip
import io
import json
import logging
import os
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

from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.device_token import DeviceToken
from zerg.services.agents import IngestResult
from zerg.services.agents import SessionIngest
from zerg.services.session_views import IngestResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])
SHIP_TRACE_HEADER = "X-Longhouse-Ship-Trace"
_TRUTHY_ENV = {"1", "true", "yes", "on"}


def _unix_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
_LIVE_INGEST_WRITER_QUEUE_HARD_LIMIT = 10
_LIVE_INGEST_ACTIVE_WRITER_GRACE_MS = 5_000.0
_ARCHIVE_INGEST_SLOTS = asyncio.Semaphore(_ARCHIVE_INGEST_MAX_IN_FLIGHT)
_ARCHIVE_INGEST_SUB_BATCH_MAX_ITEMS = 16
_ARCHIVE_INGEST_QUEUE_TIMEOUT_SECONDS = 15.0
_ARCHIVE_INGEST_WRITE_TIMEOUT_SECONDS = 15.0
_ARCHIVE_INGEST_REQUEST_BUDGET_SECONDS = 60.0
_INGEST_STAGE_HEADER_LIMIT = 8
_UNTRACED_INGEST_MAX_EVENTS = 200
_UNTRACED_INGEST_MAX_SOURCE_LINES = 200
_UNTRACED_INGEST_MAX_DECODED_BYTES = 2 * 1024 * 1024


def _ingest_chunk_for_label(label: str) -> int:
    return _INGEST_CHUNK_BY_LABEL.get(label, 200)


def _ingest_lane_for_label(label: str) -> str:
    if label == "ingest-live":
        return "live"
    if label in _ARCHIVE_INGEST_LABELS:
        return "archive"
    return "default"


def _ingest_write_timeout_for_label(label: str) -> float | None:
    if label not in _ARCHIVE_INGEST_LABELS:
        return None
    return _ARCHIVE_INGEST_WRITE_TIMEOUT_SECONDS


def _ingest_queue_timeout_for_label(label: str) -> float | None:
    if label not in _ARCHIVE_INGEST_LABELS:
        return None
    return _ARCHIVE_INGEST_QUEUE_TIMEOUT_SECONDS


def _ingest_request_budget_for_label(label: str) -> float | None:
    if label not in _ARCHIVE_INGEST_LABELS:
        return None
    return _ARCHIVE_INGEST_REQUEST_BUDGET_SECONDS


def _cap_timeout_to_remaining(timeout: float | None, remaining_seconds: float) -> float | None:
    if timeout is None:
        return None
    return min(timeout, max(0.1, remaining_seconds / 2.0))


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


def _archive_ingest_batches(
    data: SessionIngest,
    *,
    max_items: int = _ARCHIVE_INGEST_SUB_BATCH_MAX_ITEMS,
) -> list[SessionIngest]:
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
        _check_archive_ingest_wal_pressure(write_label, response)
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
    if any(state == "failed" for state in states):
        return "failed"
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


def _archive_retry_after_for_wal_pressure() -> int:
    return max(1, int(_env_float("LONGHOUSE_ARCHIVE_INGEST_WAL_RETRY_AFTER_SECONDS", 30.0)))


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


def _check_archive_ingest_wal_pressure(write_label: str, response: Response) -> None:
    if write_label not in _ARCHIVE_INGEST_LABELS:
        return

    try:
        from zerg.database import get_wal_bytes
        from zerg.services.archive_pressure import evaluate_archive_wal_pressure

        pressure = evaluate_archive_wal_pressure(get_wal_bytes())
    except Exception:
        logger.warning("Archive ingest WAL pressure check failed; allowing ingest", exc_info=True)
        return

    if pressure.wal_bytes is not None:
        response.headers["X-Ingest-Archive-Wal-Bytes"] = str(pressure.wal_bytes)
    response.headers["X-Ingest-Archive-Wal-Shed-Threshold-Bytes"] = str(pressure.threshold_bytes)
    if pressure.shed:
        _raise_archive_ingest_backpressure(
            response,
            admission_state="archive_wal_pressure",
            retry_after_seconds=_archive_retry_after_for_wal_pressure(),
        )


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

    _check_historical_archive_admission(write_label, response, admitted_bytes=0)
    _check_archive_ingest_wal_pressure(write_label, response)
    await _check_archive_ingest_writer_pressure(write_label, response)

    if _ARCHIVE_INGEST_SLOTS.locked():
        _raise_archive_ingest_backpressure(response)

    await _ARCHIVE_INGEST_SLOTS.acquire()
    return True


def _check_historical_archive_admission(write_label: str, response: Response, *, admitted_bytes: int) -> None:
    if write_label not in _ARCHIVE_INGEST_LABELS:
        return
    from zerg.metrics import historical_admission_rejections_total
    from zerg.services.historical_admission import evaluate_historical_admission

    decision = evaluate_historical_admission(
        root=get_settings().data_dir,
        admitted_bytes=admitted_bytes,
        stored_bytes=None,
        enforce_stored_ceiling=False,
    )
    if decision.admitted:
        return
    historical_admission_rejections_total.labels(path="legacy_archive", reason=decision.reason).inc()
    _raise_archive_ingest_backpressure(
        response,
        admission_state=decision.reason,
        retry_after_seconds=decision.retry_after_seconds,
    )


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


def _prepare_archive_primary_with_fresh_manifest_db(
    *,
    data: SessionIngest,
    result,
    prepare_ingest_archive,
    settings=None,
):
    from zerg.database import get_session_factory

    SessionLocal = get_session_factory()
    with SessionLocal() as read_db:
        return prepare_ingest_archive(
            data=data,
            result=result,
            settings=settings,
            manifest_db=read_db,
        )


async def _prepare_archive_primary_before_ingest(
    *,
    data: SessionIngest,
    fallback_db: Session,
    settings,
):
    """Prepare immutable archive chunks before projection writes run."""

    from zerg.services.archive_primary import prepare_ingest_archive

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
        return prepare_ingest_archive(
            data=data,
            result=result,
            settings=settings,
            manifest_db=fallback_db,
        )
    return await asyncio.to_thread(
        _prepare_archive_primary_with_fresh_manifest_db,
        data=data,
        result=result,
        prepare_ingest_archive=prepare_ingest_archive,
        settings=settings,
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
    auth_token: DeviceToken | ManagedSessionToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> IngestResponse:
    """Reject v1 transcript ingest.

    A Runtime Host accepts transcript ingest only through storage-v2; shippers
    still calling this route are told to upgrade rather than silently writing
    to the archive.
    """
    raise HTTPException(
        status_code=status.HTTP_426_UPGRADE_REQUIRED,
        detail={
            "code": "storage_v2_required",
            "message": "This Runtime Host accepts transcript ingest only through storage-v2.",
        },
    )
