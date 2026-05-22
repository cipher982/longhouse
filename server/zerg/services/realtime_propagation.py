"""Per-session realtime propagation reports from existing telemetry facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.schemas.observability import RealtimePropagationBottleneckResponse
from zerg.schemas.observability import RealtimePropagationClientRenderResponse
from zerg.schemas.observability import RealtimePropagationEventResponse
from zerg.schemas.observability import RealtimePropagationObservationRefResponse
from zerg.schemas.observability import RealtimePropagationServerFanoutResponse
from zerg.schemas.observability import RealtimePropagationSessionReportResponse
from zerg.schemas.observability import RealtimePropagationSessionResponse
from zerg.schemas.observability import RealtimePropagationShipTraceResponse
from zerg.schemas.observability import RealtimePropagationStageResponse
from zerg.services.client_render_observations import ClientRenderObservation
from zerg.services.client_render_observations import list_client_render_observations
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_observations import OBS_KIND_SERVER_FANOUT
from zerg.utils.time import normalize_utc
from zerg.utils.time import utc_now

_SHIP_TRACE_SOURCE = "agents_ingest_trace"
_SHIP_TRACE_PROGRESS_KIND = "ship_pipeline_trace"
_KNOWN_UNIMPLEMENTED_PROBES: list[str] = []
_SHIP_TRACE_RAW_ALLOWLIST = {
    "schema",
    "kind",
    "trace_id",
    "provider",
    "session_id",
    "path",
    "work_context",
    "observation_source",
    "event_count",
    "offset",
    "new_offset",
    "range_bytes",
    "http_send_started_at_ms",
    "wake_reason",
    "turn_id",
    "session_id_hint",
    "file_len_hint",
    "observed_at_ms",
    "latest_observed_at_ms",
    "wake_received_at_ms",
    "enqueued_at_ms",
    "job_started_at_ms",
    "prepare_started_at_ms",
    "prepare_finished_at_ms",
    "prepare_blocking_queue_wait_ms",
    "prepare_open_db_ms",
    "prepare_identity_ms",
    "prepare_cursor_ms",
    "prepare_binding_wait_ms",
    "prepare_parse_ms",
    "prepare_batch_build_ms",
    "observation_to_enqueue_ms",
    "observation_window_ms",
    "observation_to_wake_ms",
    "wake_to_enqueue_ms",
    "enqueue_to_job_ms",
    "observed_to_job_ms",
    "prepare_ms",
    "job_to_http_ms",
}
_CROSS_CLOCK_NOTE = "Spans clocks from different machines/processes; positive deltas may include clock skew."


@dataclass(frozen=True)
class _ObservationPayload:
    row: SessionObservation
    payload: dict[str, Any]


@dataclass(frozen=True)
class _ShipTracePayload:
    row: SessionObservation
    ship_trace: dict[str, Any]
    server_trace: dict[str, Any]


@dataclass(frozen=True)
class _RenderMatch:
    observation: ClientRenderObservation
    matched_by: str


@dataclass(frozen=True)
class _ServerFanoutPayload:
    row: SessionObservation
    payload: dict[str, Any]


def build_realtime_propagation_session_report(
    db: Session,
    *,
    session_id: UUID,
    event_limit: int = 20,
    surface: str | None = None,
) -> RealtimePropagationSessionReportResponse | None:
    """Build a best-effort propagation waterfall for recent session events.

    This is intentionally read-only and forensic. Missing probes are returned as
    explicit gaps instead of being inferred from aggregate metrics.
    """

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    limit = max(1, min(int(event_limit), 100))
    events = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .order_by(AgentEvent.timestamp.desc(), AgentEvent.id.desc())
        .limit(limit)
        .all()
    )
    provider_observations = _load_provider_observations(db, session_id=session_id, limit=max(500, limit * 20))
    ship_traces = _load_ship_traces(db, session_id=session_id, limit=max(500, limit * 20))
    server_fanouts = _load_server_fanouts(db, session_id=session_id, limit=max(500, limit * 20))
    client_renders = _load_client_renders(db, session_id=session_id, surface=surface, limit=max(500, limit * 50))

    event_reports = [
        _build_event_report(
            event,
            provider_observations=provider_observations,
            ship_traces=ship_traces,
            server_fanouts=server_fanouts,
            client_renders=client_renders,
        )
        for event in events
    ]

    gaps = _build_report_gaps(event_reports)
    return RealtimePropagationSessionReportResponse(
        generated_at=utc_now(),
        session=RealtimePropagationSessionResponse(
            session_id=str(session.id),
            provider=session.provider,
            project=session.project,
            device_id=session.device_id,
            device_name=session.device_name,
            managed_transport=session.managed_transport,
            execution_home=session.execution_home,
            started_at=session.started_at,
            last_activity_at=session.last_activity_at,
        ),
        event_limit=limit,
        surface=surface,
        events=event_reports,
        gaps=gaps,
        known_unimplemented_probes=list(_KNOWN_UNIMPLEMENTED_PROBES),
    )


def _load_provider_observations(
    db: Session,
    *,
    session_id: UUID,
    limit: int,
) -> list[_ObservationPayload]:
    rows = (
        db.query(SessionObservation)
        .filter(SessionObservation.session_id == session_id)
        .filter(SessionObservation.kind == OBS_KIND_PROVIDER_EVENT)
        .order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
        .limit(limit)
        .all()
    )
    return [_ObservationPayload(row=row, payload=_decode_payload(row)) for row in rows]


def _load_ship_traces(
    db: Session,
    *,
    session_id: UUID,
    limit: int,
) -> list[_ShipTracePayload]:
    rows = (
        db.query(SessionObservation)
        .filter(SessionObservation.session_id == session_id)
        .filter(SessionObservation.source == _SHIP_TRACE_SOURCE)
        .filter(SessionObservation.kind == OBS_KIND_RUNTIME_SIGNAL)
        .order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
        .limit(limit)
        .all()
    )
    traces: list[_ShipTracePayload] = []
    for row in rows:
        payload = _decode_payload(row)
        inner = payload.get("payload")
        if not isinstance(inner, dict) or inner.get("progress_kind") != _SHIP_TRACE_PROGRESS_KIND:
            continue
        ship_trace = inner.get("ship_trace")
        server_trace = inner.get("server_trace")
        if isinstance(ship_trace, dict):
            traces.append(
                _ShipTracePayload(
                    row=row,
                    ship_trace=ship_trace,
                    server_trace=server_trace if isinstance(server_trace, dict) else {},
                )
            )
    return traces


def _load_client_renders(
    db: Session,
    *,
    session_id: UUID,
    surface: str | None,
    limit: int,
) -> list[ClientRenderObservation]:
    return list_client_render_observations(
        db,
        session_id=session_id,
        surface=surface,
        limit=limit,
    ).rows


def _load_server_fanouts(
    db: Session,
    *,
    session_id: UUID,
    limit: int,
) -> list[_ServerFanoutPayload]:
    rows = (
        db.query(SessionObservation)
        .filter(SessionObservation.session_id == session_id)
        .filter(SessionObservation.kind == OBS_KIND_SERVER_FANOUT)
        .order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
        .limit(limit)
        .all()
    )
    return [_ServerFanoutPayload(row=row, payload=_decode_payload(row)) for row in rows]


def _build_event_report(
    event: AgentEvent,
    *,
    provider_observations: list[_ObservationPayload],
    ship_traces: list[_ShipTracePayload],
    server_fanouts: list[_ServerFanoutPayload],
    client_renders: list[ClientRenderObservation],
) -> RealtimePropagationEventResponse:
    provider_observation = _match_provider_observation(event, provider_observations)
    ship_trace = _match_ship_trace(event, ship_traces)
    server_fanout = _match_server_fanout(event, server_fanouts, ship_trace=ship_trace)
    render_matches = _match_client_renders(event, client_renders)
    first_render = render_matches[0] if render_matches else None

    provider_ts = normalize_utc(event.timestamp)
    ship_trace_response = _build_ship_trace_response(ship_trace) if ship_trace else None
    render_responses = [_build_client_render_response(match) for match in render_matches]
    first_render_response = render_responses[0] if render_responses else None

    stages = _build_stages(
        provider_ts=provider_ts,
        ship_trace=ship_trace,
        server_fanout=server_fanout,
        first_render=first_render,
    )
    bottleneck = _build_bottleneck(stages)
    gaps = _build_event_gaps(
        provider_observation=provider_observation,
        ship_trace=ship_trace,
        server_fanout=server_fanout,
        first_render=first_render,
    )
    total_provider_to_first_render_ms = None
    if provider_ts is not None and first_render is not None:
        total_provider_to_first_render_ms = _duration_ms(provider_ts, first_render.observation.row.observed_at)
    measured_total_ms = _measured_total_ms(stages)
    unaccounted_ms = (
        total_provider_to_first_render_ms - measured_total_ms
        if total_provider_to_first_render_ms is not None and measured_total_ms is not None
        else None
    )

    return RealtimePropagationEventResponse(
        event_id=int(event.id),
        role=event.role,
        timestamp=event.timestamp,
        source_path=event.source_path,
        source_offset=event.source_offset,
        event_uuid=event.event_uuid,
        event_origin=event.event_origin,
        provider_observation=(
            _build_observation_ref(provider_observation.row)
            if provider_observation is not None
            else None
        ),
        ship_trace=ship_trace_response,
        server_fanout=_build_server_fanout_response(server_fanout) if server_fanout is not None else None,
        client_renders=render_responses,
        first_client_render=first_render_response,
        total_provider_to_first_render_ms=total_provider_to_first_render_ms,
        measured_total_ms=measured_total_ms,
        unaccounted_ms=unaccounted_ms,
        client_clock_skew_ms=(
            _int_or_none(first_render.observation.payload.get("clock_skew_ms")) if first_render is not None else None
        ),
        bottleneck=bottleneck,
        stages=stages,
        gaps=gaps,
    )


def _match_provider_observation(
    event: AgentEvent,
    observations: list[_ObservationPayload],
) -> _ObservationPayload | None:
    candidates: list[tuple[int, datetime, int, _ObservationPayload]] = []
    for item in observations:
        payload = item.payload
        if event.event_uuid and (
            payload.get("event_uuid") == event.event_uuid
            or item.row.source_cursor == event.event_uuid
        ):
            candidates.append((0, _sort_dt(item.row.observed_at), int(item.row.id), item))
            continue
        if event.source_offset is not None and item.row.source_offset == event.source_offset:
            if event.source_path and item.row.source_path == event.source_path:
                candidates.append((1, _sort_dt(item.row.observed_at), int(item.row.id), item))
            elif not event.source_path and not item.row.source_path:
                candidates.append((2, _sort_dt(item.row.observed_at), int(item.row.id), item))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


def _match_ship_trace(
    event: AgentEvent,
    ship_traces: list[_ShipTracePayload],
) -> _ShipTracePayload | None:
    candidates = [trace for trace in ship_traces if _trace_covers_event(trace.ship_trace, event)]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda trace: (
            _int_or_none(trace.ship_trace.get("range_bytes")) or 2**63 - 1,
            _int_or_none(trace.ship_trace.get("event_count")) or 2**31 - 1,
            -int(trace.row.id),
        ),
    )


def _trace_covers_event(ship_trace: dict[str, Any], event: AgentEvent) -> bool:
    if event.source_offset is None:
        return False
    path = str(ship_trace.get("path") or "")
    if path and event.source_path and path != event.source_path:
        return False
    offset = _int_or_none(ship_trace.get("offset"))
    new_offset = _int_or_none(ship_trace.get("new_offset"))
    if offset is None:
        return False
    if new_offset is None:
        return event.source_offset == offset
    return offset <= event.source_offset < new_offset


def _match_server_fanout(
    event: AgentEvent,
    server_fanouts: list[_ServerFanoutPayload],
    *,
    ship_trace: _ShipTracePayload | None,
) -> _ServerFanoutPayload | None:
    trace_id = _str_or_none(ship_trace.ship_trace.get("trace_id")) if ship_trace else None
    candidates: list[tuple[int, datetime, int, _ServerFanoutPayload]] = []
    for item in server_fanouts:
        payload_trace_id = _str_or_none(item.payload.get("ship_trace_id"))
        if trace_id and payload_trace_id == trace_id:
            candidates.append((0, _sort_dt(item.row.observed_at), int(item.row.id), item))
            continue
        latest_event_id = _int_or_none(item.payload.get("latest_event_id"))
        if latest_event_id is not None and latest_event_id == int(event.id):
            candidates.append((1, _sort_dt(item.row.observed_at), int(item.row.id), item))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


def _match_client_renders(
    event: AgentEvent,
    client_renders: list[ClientRenderObservation],
) -> list[_RenderMatch]:
    matches: list[_RenderMatch] = []
    projection_item_id = f"{event.role}:{event.id}"
    direct_cursor = f"event:{event.id}"
    for item in client_renders:
        payload = item.payload
        matched_by: str | None = None
        if item.row.source_cursor == direct_cursor or str(payload.get("event_id") or "") == str(event.id):
            matched_by = "event_id"
        else:
            webkit = payload.get("webkit")
            latest_item_id = webkit.get("latest_item_id") if isinstance(webkit, dict) else None
            if latest_item_id == projection_item_id:
                matched_by = "latest_item_id"
        if matched_by is not None:
            matches.append(_RenderMatch(observation=item, matched_by=matched_by))

    return sorted(
        matches,
        key=lambda match: (
            normalize_utc(match.observation.row.observed_at) or datetime.max.replace(tzinfo=timezone.utc),
            int(match.observation.row.id),
        ),
    )


def _build_stages(
    *,
    provider_ts: datetime | None,
    ship_trace: _ShipTracePayload | None,
    server_fanout: _ServerFanoutPayload | None,
    first_render: _RenderMatch | None,
) -> list[RealtimePropagationStageResponse]:
    trace = ship_trace.ship_trace if ship_trace else {}
    server = ship_trace.server_trace if ship_trace else {}

    engine_observed = _dt_from_ms(trace.get("observed_at_ms"))
    engine_enqueued = _dt_from_ms(trace.get("enqueued_at_ms"))
    job_started = _dt_from_ms(trace.get("job_started_at_ms"))
    http_send = _dt_from_ms(trace.get("http_send_started_at_ms"))
    server_handler = _dt_from_ms(server.get("handler_entered_at_ms"))
    server_store = _dt_from_ms(server.get("store_returned_at_ms"))
    server_fanout_at = normalize_utc(server_fanout.row.observed_at) if server_fanout is not None else None
    client_received_at = (
        _client_dt_from_ms(
            first_render.observation.payload.get("client_received_at_ms"),
            first_render.observation.payload.get("clock_skew_ms"),
        )
        if first_render
        else None
    )
    client_rendered = normalize_utc(first_render.observation.row.observed_at) if first_render else None
    clock_skew_ms = _int_or_none(first_render.observation.payload.get("clock_skew_ms")) if first_render else None
    render_note = (
        "Includes server fanout, client receive, API refresh, and render until those probes are split."
    )
    if clock_skew_ms not in (None, 0):
        render_note = f"{render_note} Client render time is corrected by clock_skew_ms={clock_skew_ms}."

    stages = [
        _stage(
            "provider_to_engine_observed",
            "Provider event -> engine observed",
            provider_ts,
            engine_observed,
            source="events + ship_pipeline_trace",
            confidence="derived",
            note=_CROSS_CLOCK_NOTE,
        ),
        _stage(
            "engine_observed_to_enqueued",
            "Engine observed -> enqueued",
            engine_observed,
            engine_enqueued,
            source="ship_pipeline_trace",
        ),
        _stage(
            "engine_enqueued_to_job_started",
            "Engine enqueued -> job started",
            engine_enqueued,
            job_started,
            source="ship_pipeline_trace",
        ),
        _stage(
            "engine_job_started_to_http_send",
            "Engine job started -> HTTP send",
            job_started,
            http_send,
            source="ship_pipeline_trace",
        ),
        _stage(
            "http_send_to_server_handler",
            "HTTP send -> server handler",
            http_send,
            server_handler,
            source="ship_pipeline_trace + server_trace",
            confidence="derived",
            note=_CROSS_CLOCK_NOTE,
        ),
        _stage(
            "server_handler_to_store_returned",
            "Server handler -> store returned",
            server_handler,
            server_store,
            source="server_trace",
        ),
    ]
    if server_fanout_at is not None and client_received_at is not None:
        stages.extend(
            [
                _stage(
                    "server_store_to_fanout",
                    "Server store returned -> fanout",
                    server_store,
                    server_fanout_at,
                    source="server_trace + server_fanout",
                ),
                _stage(
                    "server_fanout_to_client_received",
                    "Server fanout -> client received",
                    server_fanout_at,
                    client_received_at,
                    source="server_fanout + client_render",
                    confidence="derived" if clock_skew_ms not in (None, 0) else "observed",
                ),
                _stage(
                    "client_received_to_rendered",
                    "Client received -> rendered",
                    client_received_at,
                    client_rendered,
                    source="client_render",
                    confidence="derived" if clock_skew_ms not in (None, 0) else "observed",
                ),
            ]
        )
    else:
        stages.append(
            _stage(
                "server_store_to_client_rendered",
                "Server store returned -> client rendered",
                server_store,
                client_rendered,
                source="server_trace + client_render",
                confidence="derived" if clock_skew_ms not in (None, 0) else "observed",
                note=render_note,
            )
        )
    return stages


def _stage(
    key: str,
    label: str,
    started_at: datetime | None,
    ended_at: datetime | None,
    *,
    source: str,
    confidence: Literal["observed", "derived"] = "observed",
    note: str | None = None,
) -> RealtimePropagationStageResponse:
    duration_ms = _duration_ms(started_at, ended_at)
    resolved_confidence: Literal["observed", "derived", "missing"] = (
        confidence if duration_ms is not None else "missing"
    )
    resolved_note = note
    if duration_ms is not None and duration_ms < 0:
        duration_ms = 0
        resolved_confidence = "derived"
        skew_note = "Negative measured delta clamped to 0; clocks may be skewed."
        resolved_note = f"{resolved_note} {skew_note}" if resolved_note else skew_note
    return RealtimePropagationStageResponse(
        key=key,
        label=label,
        status="measured" if duration_ms is not None else "missing",
        confidence=resolved_confidence,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        source=source,
        note=resolved_note,
    )


def _build_bottleneck(
    stages: list[RealtimePropagationStageResponse],
) -> RealtimePropagationBottleneckResponse | None:
    measured = [stage for stage in stages if stage.duration_ms is not None]
    if not measured:
        return None
    slowest = max(measured, key=lambda stage: int(stage.duration_ms or 0))
    return RealtimePropagationBottleneckResponse(
        stage_key=slowest.key,
        label=slowest.label,
        duration_ms=int(slowest.duration_ms or 0),
    )


def _measured_total_ms(stages: list[RealtimePropagationStageResponse]) -> int | None:
    measured = [stage.duration_ms for stage in stages if stage.duration_ms is not None]
    if not measured:
        return None
    return int(sum(measured))


def _build_event_gaps(
    *,
    provider_observation: _ObservationPayload | None,
    ship_trace: _ShipTracePayload | None,
    server_fanout: _ServerFanoutPayload | None,
    first_render: _RenderMatch | None,
) -> list[str]:
    gaps: list[str] = []
    if provider_observation is None:
        gaps.append("missing_provider_event_observation")
    if ship_trace is None:
        gaps.append("missing_ship_pipeline_trace")
    if server_fanout is None:
        gaps.append("missing_server_fanout_observation")
    if first_render is None:
        gaps.append("missing_client_render_beacon")
    elif first_render.observation.payload.get("client_received_at_ms") is None:
        gaps.append("missing_client_received_timestamp")
    return gaps


def _build_report_gaps(
    events: list[RealtimePropagationEventResponse],
) -> list[str]:
    gaps: list[str] = []
    if not events:
        gaps.append("no_durable_events_in_window")
        return gaps
    if all(event.ship_trace is None for event in events):
        gaps.append("no_ship_pipeline_traces_in_window")
    if all(event.first_client_render is None for event in events):
        gaps.append("no_client_render_beacons_in_window")
    if all(event.provider_observation is None for event in events):
        gaps.append("no_provider_event_observations_in_window")
    if all(event.server_fanout is None for event in events):
        gaps.append("no_server_fanout_observations_in_window")
    return gaps


def _build_observation_ref(row: SessionObservation) -> RealtimePropagationObservationRefResponse:
    return RealtimePropagationObservationRefResponse(
        observation_id=row.observation_id,
        source=row.source,
        kind=row.kind,
        observed_at=row.observed_at,
        received_at=row.received_at,
        source_offset=row.source_offset,
        source_cursor=row.source_cursor,
    )


def _build_ship_trace_response(trace: _ShipTracePayload) -> RealtimePropagationShipTraceResponse:
    ship = trace.ship_trace
    server = trace.server_trace
    return RealtimePropagationShipTraceResponse(
        trace_id=str(ship.get("trace_id") or ""),
        work_context=_str_or_none(ship.get("work_context")),
        observation_source=_str_or_none(ship.get("observation_source")),
        event_count=_int_or_none(ship.get("event_count")),
        offset=_int_or_none(ship.get("offset")),
        new_offset=_int_or_none(ship.get("new_offset")),
        range_bytes=_int_or_none(ship.get("range_bytes")),
        observed_at=_dt_from_ms(ship.get("observed_at_ms")),
        enqueued_at=_dt_from_ms(ship.get("enqueued_at_ms")),
        job_started_at=_dt_from_ms(ship.get("job_started_at_ms")),
        http_send_started_at=_dt_from_ms(ship.get("http_send_started_at_ms")),
        server_handler_entered_at=_dt_from_ms(server.get("handler_entered_at_ms")),
        server_store_returned_at=_dt_from_ms(server.get("store_returned_at_ms")),
        observation_to_enqueue_ms=_int_or_none(ship.get("observation_to_enqueue_ms")),
        enqueue_to_job_ms=_int_or_none(ship.get("enqueue_to_job_ms")),
        job_to_http_ms=_int_or_none(ship.get("job_to_http_ms")),
        server_store_write_ms=_int_or_none(server.get("store_write_ms")),
        raw=_redact_ship_trace_raw(ship),
        raw_dropped_keys=len(set(ship.keys()) - _SHIP_TRACE_RAW_ALLOWLIST),
    )


def _build_client_render_response(match: _RenderMatch) -> RealtimePropagationClientRenderResponse:
    payload = match.observation.payload
    webkit = payload.get("webkit") if isinstance(payload.get("webkit"), dict) else {}
    return RealtimePropagationClientRenderResponse(
        surface=str(payload.get("surface") or "unknown"),
        event_id=_str_or_none(payload.get("event_id")),
        matched_by=match.matched_by,
        observed_at=match.observation.row.observed_at,
        received_at=match.observation.row.received_at,
        emitted_at_ms=_int_or_none(payload.get("emitted_at_ms")),
        rendered_at_ms=_int_or_none(payload.get("rendered_at_ms")),
        clock_skew_ms=_int_or_none(payload.get("clock_skew_ms")),
        server_fanout_at_ms=_int_or_none(payload.get("server_fanout_at_ms")),
        client_received_at_ms=_int_or_none(payload.get("client_received_at_ms")),
        pubsub_seq=_int_or_none(payload.get("pubsub_seq")),
        latency_ms=_int_or_none(payload.get("latency_ms")),
        webkit_stage=_str_or_none(webkit.get("stage")) if isinstance(webkit, dict) else None,
        latest_item_id=_str_or_none(webkit.get("latest_item_id")) if isinstance(webkit, dict) else None,
    )


def _build_server_fanout_response(item: _ServerFanoutPayload) -> RealtimePropagationServerFanoutResponse:
    payload = item.payload
    return RealtimePropagationServerFanoutResponse(
        observation_id=item.row.observation_id,
        observed_at=item.row.observed_at,
        received_at=item.row.received_at,
        latest_event_id=_int_or_none(payload.get("latest_event_id")),
        server_fanout_at_ms=_int_or_none(payload.get("server_fanout_at_ms")),
        session_pubsub_seq=_int_or_none(payload.get("session_pubsub_seq")),
        timeline_pubsub_seq=_int_or_none(payload.get("timeline_pubsub_seq")),
        ship_trace_id=_str_or_none(payload.get("ship_trace_id")),
    )


def _redact_ship_trace_raw(ship_trace: dict[str, Any]) -> dict[str, Any]:
    """Keep trace metadata, never transcript payload content."""

    return {key: value for key, value in ship_trace.items() if key in _SHIP_TRACE_RAW_ALLOWLIST}


def _decode_payload(row: SessionObservation) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _dt_from_ms(value: Any) -> datetime | None:
    raw = _int_or_none(value)
    if raw is None:
        return None
    return datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)


def _client_dt_from_ms(value: Any, clock_skew_ms: Any) -> datetime | None:
    raw = _int_or_none(value)
    if raw is None:
        return None
    skew = _int_or_none(clock_skew_ms) or 0
    return datetime.fromtimestamp((raw - skew) / 1000.0, tz=timezone.utc)


def _duration_ms(started_at: datetime | None, ended_at: datetime | None) -> int | None:
    start = normalize_utc(started_at)
    end = normalize_utc(ended_at)
    if start is None or end is None:
        return None
    return int(round((end - start).total_seconds() * 1000))


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sort_dt(value: datetime | None) -> datetime:
    return normalize_utc(value) or datetime.max.replace(tzinfo=timezone.utc)
