"""Browser timeline session SSE stream use case."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from time import monotonic
from typing import Protocol
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.models.agents import SessionRuntimeEvent
from zerg.services.agents_store import AgentsStore
from zerg.services.session_listing import SessionListingError
from zerg.services.session_pubsub import TOPIC_TIMELINE
from zerg.services.session_pubsub import get_pubsub
from zerg.services.timeline_session_listing import TimelineSessionCardResponse
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import TimelineSessionsListResponse
from zerg.services.timeline_session_listing import list_timeline_sessions_for_browser

logger = logging.getLogger(__name__)

# Pubsub wakes active tabs immediately. This timeout is only the fallback poll
# for missed cross-worker wakes, so keep it slow enough that idle tabs do not
# continuously compete with machine ingest for SQLite connections.
TIMELINE_STREAM_CHANGE_WAIT_SECONDS = 5.0
TIMELINE_STREAM_HEARTBEAT_SECONDS = 30.0

TimelineWindowSignature = tuple[
    tuple[str, str, datetime | None, datetime | None, datetime | None, int, int | None],
    ...,
]


class TimelineStreamRequest(Protocol):
    async def is_disconnected(self) -> bool: ...


def validate_timeline_stream_contract(*, query: str | None, sort: str | None, mode: str | None) -> None:
    if _stream_supports_preflight(query=query, sort=sort, mode=mode):
        return
    raise SessionListingError(
        400,
        "Timeline session stream only supports the default no-query lexical recency contract.",
    )


async def stream_timeline_sessions_for_browser(
    request: TimelineStreamRequest,
    *,
    session_factory: sessionmaker,
    params: TimelineSessionListParams,
    skip_initial_replay: bool,
):
    previous_signatures: dict[str, str] = {}
    previous_window_signature: TimelineWindowSignature | None = None
    last_heartbeat = monotonic()
    preflight_enabled = _stream_supports_preflight(query=params.query, sort=params.sort, mode=params.mode)
    bus = get_pubsub()
    timeline_seq = bus.peek_latest_seq(TOPIC_TIMELINE)

    yield {
        "event": "connected",
        "data": json.dumps({"message": "Timeline session stream connected"}),
    }

    with bus.subscribe(TOPIC_TIMELINE, since_seq=timeline_seq) as timeline_subscription:
        while True:
            if await request.is_disconnected():
                logger.info("Timeline sessions SSE disconnected")
                break

            if skip_initial_replay:
                # The browser already has a fresh timeline snapshot from the initial
                # HTTP query. Do not immediately rebuild the same window on stream
                # connect; wait for the next write/heartbeat cycle instead.
                skip_initial_replay = False
                await _wait_for_timeline_change(timeline_subscription)
                continue

            if preflight_enabled:
                with session_factory() as db:
                    current_window_signature = _load_timeline_stream_window_signature(db=db, params=params)
                if previous_window_signature is not None and current_window_signature == previous_window_signature:
                    now = monotonic()
                    if now - last_heartbeat >= TIMELINE_STREAM_HEARTBEAT_SECONDS:
                        yield {
                            "event": "heartbeat",
                            "data": json.dumps({"timestamp": _utc_now_z()}),
                        }
                        last_heartbeat = now
                    await _wait_for_timeline_change(timeline_subscription)
                    continue
                previous_window_signature = current_window_signature

            with session_factory() as db:
                result = await list_timeline_sessions_for_browser(db=db, params=params)
                response = _expect_threaded_response(result.response, compatibility_raw=result.compatibility_raw)

            current_payloads: dict[str, dict] = {}
            current_signatures: dict[str, str] = {}

            for session in response.sessions:
                payload, signature = _session_payload_signature(session)
                current_payloads[session.thread_id] = payload
                current_signatures[session.thread_id] = signature

            removed_ids = previous_signatures.keys() - current_signatures.keys()
            for thread_id in sorted(removed_ids):
                yield {
                    "event": "session_remove",
                    "data": json.dumps(
                        {
                            "thread_id": thread_id,
                            "total": response.total,
                            "has_real_sessions": response.has_real_sessions,
                        }
                    ),
                }

            for session in response.sessions:
                signature = current_signatures[session.thread_id]
                if previous_signatures.get(session.thread_id) == signature:
                    continue
                yield {
                    "event": "session_upsert",
                    "data": json.dumps(
                        {
                            "session": current_payloads[session.thread_id],
                            "total": response.total,
                            "has_real_sessions": response.has_real_sessions,
                        }
                    ),
                }

            previous_signatures = current_signatures

            now = monotonic()
            if now - last_heartbeat >= TIMELINE_STREAM_HEARTBEAT_SECONDS:
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"timestamp": _utc_now_z()}),
                }
                last_heartbeat = now

            await _wait_for_timeline_change(timeline_subscription)


def _expect_threaded_response(
    response: object,
    *,
    compatibility_raw: bool,
) -> TimelineSessionsListResponse:
    if compatibility_raw or not isinstance(response, TimelineSessionsListResponse):
        raise RuntimeError("Timeline stream received a raw compatibility response")
    return response


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _session_payload_signature(session: TimelineSessionCardResponse) -> tuple[dict, str]:
    payload = session.model_dump(mode="json")
    return payload, json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _effective_stream_sort(query: str | None, sort: str | None) -> str:
    return sort or ("relevance" if query else "recency")


def _stream_supports_preflight(*, query: str | None, sort: str | None, mode: str | None) -> bool:
    effective_sort = _effective_stream_sort(query, sort)
    return query is None and mode in (None, "lexical") and effective_sort == "recency"


def _load_timeline_stream_window_signature(
    *,
    db: Session,
    params: TimelineSessionListParams,
) -> TimelineWindowSignature:
    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=params.days_back)
    _, rows = store.list_timeline_thread_window_signature(
        project=params.project,
        provider=params.provider,
        environment=params.environment,
        include_test=params.include_test,
        device_id=params.device_id,
        since=since,
        query=params.query,
        limit=params.limit,
        offset=params.offset,
        hide_autonomous=params.hide_autonomous,
        context_mode=params.context_mode,
        include_total=False,
    )
    overlay_heads = _load_live_transcript_overlay_heads(db=db, rows=rows)
    return tuple((*row, overlay_heads.get(row[1])) for row in rows)


def _load_live_transcript_overlay_heads(
    *,
    db: Session,
    rows: tuple[tuple[str, str, datetime | None, datetime | None, datetime | None, int], ...],
) -> dict[str, int]:
    session_ids: list[UUID] = []
    for row in rows:
        try:
            session_ids.append(UUID(row[1]))
        except (TypeError, ValueError):
            continue
    if not session_ids:
        return {}

    result_rows = (
        db.query(
            SessionRuntimeEvent.session_id.label("session_id"),
            func.max(SessionRuntimeEvent.id).label("max_id"),
        )
        .filter(SessionRuntimeEvent.session_id.in_(session_ids))
        .filter(SessionRuntimeEvent.source == "codex_bridge_live")
        .filter(SessionRuntimeEvent.kind == "progress_signal")
        .group_by(SessionRuntimeEvent.session_id)
        .all()
    )
    heads: dict[str, int] = {}
    for row in result_rows:
        if row.session_id is None or row.max_id is None:
            continue
        heads[str(row.session_id)] = int(row.max_id)
    return heads


async def _wait_for_timeline_change(subscription=None) -> None:
    """Wait for a timeline publish or timeout before the next SSE poll cycle."""
    if subscription is not None:
        await subscription.next_message(timeout=TIMELINE_STREAM_CHANGE_WAIT_SECONDS)
        return

    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()
    if ws.is_configured:
        await ws.wait_for_change(timeout=TIMELINE_STREAM_CHANGE_WAIT_SECONDS)
    else:
        await asyncio.sleep(TIMELINE_STREAM_CHANGE_WAIT_SECONDS)
