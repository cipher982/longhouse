"""Shared observability view builders for machine and browser routes."""

from __future__ import annotations

from collections import Counter
from collections import defaultdict

from zerg.schemas.observability import MachineHealthItemResponse
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatusCountsResponse
from zerg.schemas.observability import ManagedTurnProviderSummaryResponse
from zerg.schemas.observability import ManagedTurnsSummaryEnvelopeResponse
from zerg.schemas.observability import ManagedTurnSummaryResponse
from zerg.schemas.observability import ObservabilityOverviewResponse
from zerg.schemas.observability import SlowTurnItemResponse
from zerg.schemas.observability import SlowTurnMachineResponse
from zerg.schemas.observability import SlowTurnsListResponse
from zerg.schemas.observability import TurnLatencyPercentilesResponse
from zerg.services.agent_heartbeat_health import MachineTransportHealthSummary
from zerg.services.session_turns import ManagedCompletedTurnSummary
from zerg.services.session_views import build_session_turn_timing_response
from zerg.utils.time import utc_now


def build_machine_health_item_response(item: MachineTransportHealthSummary) -> MachineHealthItemResponse:
    return MachineHealthItemResponse(
        device_id=item.device_id,
        version=item.version,
        last_heartbeat_at=item.last_heartbeat_at,
        heartbeat_age_seconds=item.heartbeat_age_seconds,
        stale_after_seconds=item.stale_after_seconds,
        is_stale=item.is_stale,
        status=item.status,  # type: ignore[arg-type]
        status_reason=item.status_reason,
        status_summary=item.status_summary,
        reasons=list(item.reasons),
        last_ship_at=item.last_ship_at,
        last_ship_attempt_at=item.last_ship_attempt_at,
        last_ship_result=item.last_ship_result,
        last_ship_latency_ms=item.last_ship_latency_ms,
        last_ship_http_status=item.last_ship_http_status,
        last_ship_error_kind=item.last_ship_error_kind,
        last_ship_error_message=item.last_ship_error_message,
        ship_attempts_1h=item.ship_attempts_1h,
        ship_successes_1h=item.ship_successes_1h,
        ship_success_rate_1h=item.ship_success_rate_1h,
        ship_rate_limited_1h=item.ship_rate_limited_1h,
        ship_server_errors_1h=item.ship_server_errors_1h,
        ship_payload_rejections_1h=item.ship_payload_rejections_1h,
        ship_payload_too_large_1h=item.ship_payload_too_large_1h,
        ship_retryable_client_errors_1h=item.ship_retryable_client_errors_1h,
        ship_connect_errors_1h=item.ship_connect_errors_1h,
        ship_latency_p50_ms_1h=item.ship_latency_p50_ms_1h,
        ship_latency_p95_ms_1h=item.ship_latency_p95_ms_1h,
        spool_pending=item.spool_pending,
        spool_dead=item.spool_dead,
        parse_errors_1h=item.parse_errors_1h,
        consecutive_failures=item.consecutive_failures,
        disk_free_bytes=item.disk_free_bytes,
        is_offline=item.is_offline,
    )


def build_machine_health_list_response(
    summaries: list[MachineTransportHealthSummary],
    *,
    total: int,
) -> MachineHealthListResponse:
    return MachineHealthListResponse(
        machines=[build_machine_health_item_response(item) for item in summaries],
        total=total,
    )


def build_machine_health_status_counts_response(
    summaries: list[MachineTransportHealthSummary],
) -> MachineHealthStatusCountsResponse:
    counts = Counter(item.status for item in summaries)
    return MachineHealthStatusCountsResponse(
        total=len(summaries),
        healthy=int(counts.get("healthy", 0)),
        degraded=int(counts.get("degraded", 0)),
        offline=int(counts.get("offline", 0)),
        broken=int(counts.get("broken", 0)),
    )


def build_slow_turn_item_response(item: ManagedCompletedTurnSummary) -> SlowTurnItemResponse:
    return SlowTurnItemResponse(
        turn_id=int(item.turn.id),
        session_id=str(item.session.id),
        request_id=item.turn.request_id,
        provider=item.session.provider,
        project=item.session.project,
        device_id=item.session.device_id,
        device_name=item.session.device_name,
        managed_transport=item.session.managed_transport,
        state=item.turn.state,
        terminal_phase=item.turn.terminal_phase,
        error_code=item.turn.error_code,
        user_submitted_at=item.turn.user_submitted_at,
        completed_at=item.completed_at,
        total_turn_time_ms=item.total_turn_time_ms,
        timing=build_session_turn_timing_response(item.turn),
        machine=(
            SlowTurnMachineResponse(
                device_id=item.machine.device_id,
                status=item.machine.status,  # type: ignore[arg-type]
                status_reason=item.machine.status_reason,
                status_summary=item.machine.status_summary,
                last_heartbeat_at=item.machine.last_heartbeat_at,
                heartbeat_age_seconds=item.machine.heartbeat_age_seconds,
                is_stale=item.machine.is_stale,
                version=item.machine.version,
            )
            if item.machine is not None
            else None
        ),
    )


def build_slow_turns_list_response(
    summaries: list[ManagedCompletedTurnSummary],
    *,
    total: int,
    hours_back: int,
    min_total_turn_time_ms: int,
) -> SlowTurnsListResponse:
    return SlowTurnsListResponse(
        turns=[build_slow_turn_item_response(item) for item in summaries],
        total=total,
        hours_back=hours_back,
        min_total_turn_time_ms=min_total_turn_time_ms,
    )


def build_managed_turn_summary_response(
    summaries: list[ManagedCompletedTurnSummary],
    *,
    slow_threshold_ms: int,
) -> ManagedTurnSummaryResponse:
    timing_fields = {
        "submit_to_send_ms": [],
        "submit_to_active_ms": [],
        "submit_to_terminal_ms": [],
        "active_to_terminal_ms": [],
        "terminal_to_durable_ms": [],
        "total_turn_time_ms": [],
    }
    durable_turns = 0
    terminal_only_turns = 0
    slow_turns = 0

    for item in summaries:
        timing = build_session_turn_timing_response(item.turn)
        for field_name, values in timing_fields.items():
            value = getattr(timing, field_name)
            if value is not None:
                values.append(int(value))
        if item.turn.durable_at is not None:
            durable_turns += 1
        else:
            terminal_only_turns += 1
        if item.total_turn_time_ms >= slow_threshold_ms:
            slow_turns += 1

    return ManagedTurnSummaryResponse(
        completed_turns=len(summaries),
        slow_turns=slow_turns,
        durable_turns=durable_turns,
        terminal_only_turns=terminal_only_turns,
        submit_to_send_ms=_build_latency_percentiles(timing_fields["submit_to_send_ms"]),
        submit_to_active_ms=_build_latency_percentiles(timing_fields["submit_to_active_ms"]),
        submit_to_terminal_ms=_build_latency_percentiles(timing_fields["submit_to_terminal_ms"]),
        active_to_terminal_ms=_build_latency_percentiles(timing_fields["active_to_terminal_ms"]),
        terminal_to_durable_ms=_build_latency_percentiles(timing_fields["terminal_to_durable_ms"]),
        total_turn_time_ms=_build_latency_percentiles(timing_fields["total_turn_time_ms"]),
    )


def build_managed_turns_summary_envelope_response(
    summaries: list[ManagedCompletedTurnSummary],
    *,
    hours_back: int,
    slow_threshold_ms: int,
) -> ManagedTurnsSummaryEnvelopeResponse:
    grouped: dict[str, list[ManagedCompletedTurnSummary]] = defaultdict(list)
    for item in summaries:
        grouped[item.session.provider].append(item)

    provider_rows = [
        ManagedTurnProviderSummaryResponse(
            provider=provider_key,
            **build_managed_turn_summary_response(group_items, slow_threshold_ms=slow_threshold_ms).model_dump(),
        )
        for provider_key, group_items in sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    ]

    return ManagedTurnsSummaryEnvelopeResponse(
        hours_back=hours_back,
        slow_threshold_ms=slow_threshold_ms,
        summary=build_managed_turn_summary_response(summaries, slow_threshold_ms=slow_threshold_ms),
        providers=provider_rows,
    )


def build_observability_overview_response(
    *,
    turn_summaries: list[ManagedCompletedTurnSummary],
    machine_summaries: list[MachineTransportHealthSummary],
    hours_back: int,
    slow_threshold_ms: int,
    stale_after_seconds: int,
    machine_limit: int,
    slow_turn_limit: int,
) -> ObservabilityOverviewResponse:
    slow_turns = [item for item in turn_summaries if item.total_turn_time_ms >= slow_threshold_ms]
    slow_turns.sort(
        key=lambda item: (
            -item.total_turn_time_ms,
            -item.completed_at.timestamp(),
            -int(item.turn.id),
        )
    )
    turns_summary = build_managed_turns_summary_envelope_response(
        turn_summaries,
        hours_back=hours_back,
        slow_threshold_ms=slow_threshold_ms,
    )
    return ObservabilityOverviewResponse(
        generated_at=utc_now(),
        hours_back=hours_back,
        slow_threshold_ms=slow_threshold_ms,
        stale_after_seconds=stale_after_seconds,
        summary=turns_summary.summary,
        providers=turns_summary.providers,
        machines=[build_machine_health_item_response(item) for item in machine_summaries[:machine_limit]],
        machine_counts=build_machine_health_status_counts_response(machine_summaries),
        slow_turns=[build_slow_turn_item_response(item) for item in slow_turns[:slow_turn_limit]],
        slow_turn_total=len(slow_turns),
    )


def _build_latency_percentiles(values: list[int]) -> TurnLatencyPercentilesResponse:
    clean_values = sorted(int(value) for value in values if value is not None)
    if not clean_values:
        return TurnLatencyPercentilesResponse()
    return TurnLatencyPercentilesResponse(
        p50=_percentile(clean_values, 50),
        p95=_percentile(clean_values, 95),
        max=clean_values[-1],
    )


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    k = (len(values) - 1) * (percentile / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return int(values[f])
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return int(round(d0 + d1))
