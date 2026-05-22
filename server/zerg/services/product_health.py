"""Product health checks derived from persisted session observations."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from zerg.schemas.observability import ProductHealthCheckEvidenceRefResponse
from zerg.schemas.observability import ProductHealthCheckListResponse
from zerg.schemas.observability import ProductHealthCheckLivePreviewCellResponse
from zerg.schemas.observability import ProductHealthCheckLivePreviewDimensionResponse
from zerg.schemas.observability import ProductHealthCheckLivePreviewResponse
from zerg.schemas.observability import ProductHealthCheckLivePreviewSignalsResponse
from zerg.schemas.observability import ProductHealthCheckSummaryResponse
from zerg.schemas.observability import ProductHealthCheckThresholdsResponse
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import list_machine_transport_health
from zerg.services.client_render_observations import ClientRenderObservation
from zerg.services.client_render_observations import ClientRenderObservationList
from zerg.services.client_render_observations import list_client_render_observations
from zerg.utils.time import utc_now

MACHINE_CONNECTED_CHECK_ID = "machine_connected"
RENDER_FRESHNESS_CHECK_ID = "render_freshness"
LIVE_PREVIEW_CHECK_ID = "live_preview"
RENDER_FRESHNESS_OK_SECONDS = 5 * 60
LIVE_PREVIEW_RENDER_P95_OK_MS = 500
LIVE_PREVIEW_RENDER_P95_FAILING_MS = 1_500
LIVE_PREVIEW_EVIDENCE_LIMIT = 5
LIVE_PREVIEW_OBSERVATION_LIMIT = 5_000

_WINDOW_RE = re.compile(r"^\s*(?P<count>\d+)\s*(?P<unit>[mhd])\s*$", re.IGNORECASE)
_MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class _Window:
    label: str
    delta: timedelta


@dataclass(frozen=True)
class _CellKey:
    provider: str | None
    surface: str | None
    managed: bool | None


def build_product_health_checks(
    db: Session,
    *,
    window: str = "15m",
    provider: str | None = None,
    surface: str | None = None,
    managed: bool | None = None,
) -> ProductHealthCheckListResponse:
    resolved_window = _parse_window(window)
    generated_at = utc_now()
    since = generated_at - resolved_window.delta
    render_observations = list_client_render_observations(
        db,
        since=since,
        provider=provider,
        surface=surface,
        managed=managed,
        limit=LIVE_PREVIEW_OBSERVATION_LIMIT,
    )
    live_preview = build_live_preview_check(
        db,
        resolved_window=resolved_window,
        generated_at=generated_at,
        observations=render_observations,
        provider=provider,
        surface=surface,
        managed=managed,
    )
    return ProductHealthCheckListResponse(
        checks=[
            _build_machine_connected_summary(db, window=resolved_window, generated_at=generated_at),
            _build_render_freshness_summary(
                render_observations.rows,
                window=resolved_window,
                generated_at=generated_at,
            ),
            _summarize_live_preview_check(live_preview),
        ]
    )


def build_live_preview_check(
    db: Session,
    *,
    window: str = "15m",
    resolved_window: _Window | None = None,
    generated_at: datetime | None = None,
    observations: ClientRenderObservationList | None = None,
    provider: str | None = None,
    surface: str | None = None,
    managed: bool | None = None,
) -> ProductHealthCheckLivePreviewResponse:
    resolved_window = resolved_window or _parse_window(window)
    generated_at = generated_at or utc_now()
    if observations is None:
        since = generated_at - resolved_window.delta
        observations = list_client_render_observations(
            db,
            since=since,
            provider=provider,
            surface=surface,
            managed=managed,
            limit=LIVE_PREVIEW_OBSERVATION_LIMIT,
        )
    cells = _build_live_preview_cells(
        observations.rows,
        truncated=observations.truncated,
        provider=provider,
        surface=surface,
        managed=managed,
    )
    return ProductHealthCheckLivePreviewResponse(
        check=LIVE_PREVIEW_CHECK_ID,
        window=resolved_window.label,
        generated_at=generated_at,
        cells=cells,
    )


def _build_live_preview_cells(
    observations: list[ClientRenderObservation],
    *,
    truncated: bool,
    provider: str | None,
    surface: str | None,
    managed: bool | None,
) -> list[ProductHealthCheckLivePreviewCellResponse]:
    grouped: dict[_CellKey, list[ClientRenderObservation]] = defaultdict(list)
    for observation in observations:
        grouped[
            _CellKey(
                provider=observation.provider,
                surface=observation.surface,
                managed=observation.managed,
            )
        ].append(observation)

    if not grouped:
        return [
            _build_live_preview_cell(
                _CellKey(provider=provider, surface=surface, managed=managed),
                [],
                truncated=truncated,
            )
        ]

    return [
        _build_live_preview_cell(key, rows, truncated=truncated)
        for key, rows in sorted(
            grouped.items(),
            key=lambda item: (
                item[0].provider or "",
                item[0].surface or "",
                str(item[0].managed),
            ),
        )
    ]


def _build_live_preview_cell(
    key: _CellKey,
    rows: list[ClientRenderObservation],
    *,
    truncated: bool,
) -> ProductHealthCheckLivePreviewCellResponse:
    latency_values = sorted(row.latency_ms for row in rows if row.latency_ms is not None)
    ios_render_values = sorted(
        row.ios_render_duration_ms for row in rows if row.surface == "ios" and row.ios_render_duration_ms is not None
    )
    sessions = {row.session_id for row in rows if row.session_id}
    signals = ProductHealthCheckLivePreviewSignalsResponse(
        events=len(rows),
        sessions=len(sessions),
        render_p50_ms=_percentile(latency_values, 50),
        render_p95_ms=_percentile(latency_values, 95),
        render_max_ms=latency_values[-1] if latency_values else None,
        ios_render_duration_events=len(ios_render_values),
        ios_render_duration_p50_ms=_percentile(ios_render_values, 50),
        ios_render_duration_p95_ms=_percentile(ios_render_values, 95),
        ios_render_duration_max_ms=ios_render_values[-1] if ios_render_values else None,
    )
    missing = _missing_live_preview_signals(
        key, rows=rows, latency_values=latency_values, ios_render_values=ios_render_values
    )
    coverage = _coverage_for_missing(rows, missing)
    verdict = _verdict_for_live_preview(coverage=coverage, render_p95_ms=signals.render_p95_ms)
    return ProductHealthCheckLivePreviewCellResponse(
        dimension=ProductHealthCheckLivePreviewDimensionResponse(
            provider=key.provider,
            surface=key.surface,
            managed=key.managed,
        ),
        applicable=True,
        coverage=coverage,
        verdict=verdict,
        truncated=truncated,
        signals=signals,
        thresholds=_live_preview_thresholds(),
        missing=missing,
        evidence_refs=_live_preview_evidence(rows, verdict=verdict),
    )


def _missing_live_preview_signals(
    key: _CellKey,
    *,
    rows: list[ClientRenderObservation],
    latency_values: list[int],
    ios_render_values: list[int],
) -> list[str]:
    missing: list[str] = []
    if not rows:
        missing.append("client_render_observations")
        return missing
    if not latency_values:
        missing.append("latency_ms")
    if key.surface == "ios" and not ios_render_values:
        missing.append("ios_render_duration_ms")
    return missing


def _coverage_for_missing(rows: list[ClientRenderObservation], missing: list[str]) -> str:
    if not rows:
        return "none"
    return "partial" if missing else "full"


def _verdict_for_live_preview(*, coverage: str, render_p95_ms: int | None) -> str:
    if coverage == "none" or render_p95_ms is None:
        return "unknown"
    if render_p95_ms >= LIVE_PREVIEW_RENDER_P95_FAILING_MS:
        return "failing"
    if render_p95_ms > LIVE_PREVIEW_RENDER_P95_OK_MS:
        return "degraded"
    return "ok"


def _live_preview_evidence(
    rows: list[ClientRenderObservation],
    *,
    verdict: str,
) -> list[ProductHealthCheckEvidenceRefResponse]:
    ranked = sorted(
        (row for row in rows if row.latency_ms is not None),
        key=lambda row: row.latency_ms or 0,
        reverse=True,
    )
    refs: list[ProductHealthCheckEvidenceRefResponse] = []
    seen: set[str] = set()
    for row in ranked:
        ref_id = row.session_id or row.observation_id
        if ref_id in seen:
            continue
        seen.add(ref_id)
        reason = "slow_render" if verdict in {"degraded", "failing"} else "highest_latency"
        refs.append(
            ProductHealthCheckEvidenceRefResponse(
                kind="session" if row.session_id else "observation",
                id=ref_id,
                reason=reason,
                latency_ms=row.latency_ms,
            )
        )
        if len(refs) >= LIVE_PREVIEW_EVIDENCE_LIMIT:
            break
    return refs


def _live_preview_thresholds() -> ProductHealthCheckThresholdsResponse:
    return ProductHealthCheckThresholdsResponse(
        render_p95_ms_ok=LIVE_PREVIEW_RENDER_P95_OK_MS,
        render_p95_ms_failing=LIVE_PREVIEW_RENDER_P95_FAILING_MS,
    )


def _build_machine_connected_summary(
    db: Session,
    *,
    window: _Window,
    generated_at: datetime,
) -> ProductHealthCheckSummaryResponse:
    window_seconds = max(1, int(window.delta.total_seconds()))
    machines, total = list_machine_transport_health(
        db,
        stale_after_seconds=DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
        recent_within_seconds=window_seconds,
        limit=10_000,
    )
    if total == 0:
        return ProductHealthCheckSummaryResponse(
            check=MACHINE_CONNECTED_CHECK_ID,
            verdict="unknown",
            coverage="none",
            window=window.label,
            generated_at=generated_at,
            headline=f"No machine heartbeats in the last {window.label}.",
        )

    healthy = sum(1 for machine in machines if machine.status == "healthy")
    broken = sum(1 for machine in machines if machine.status == "broken")
    unhealthy = total - healthy
    if healthy == total:
        verdict = "ok"
        headline = _machine_connected_headline(total=total, healthy=healthy, unhealthy=0)
    elif healthy == 0 and broken == total:
        verdict = "failing"
        headline = f"All {total} recent machine connection{'' if total == 1 else 's'} are broken."
    else:
        # Product-level health should flag partial machine impact without
        # declaring the whole runtime unusable while at least one machine is healthy.
        verdict = "degraded"
        headline = _machine_connected_headline(total=total, healthy=healthy, unhealthy=unhealthy)

    return ProductHealthCheckSummaryResponse(
        check=MACHINE_CONNECTED_CHECK_ID,
        verdict=verdict,
        coverage="full",
        window=window.label,
        generated_at=generated_at,
        headline=headline,
    )


def _machine_connected_headline(*, total: int, healthy: int, unhealthy: int) -> str:
    machine_label = "machine" if total == 1 else "machines"
    if unhealthy == 0:
        return f"{healthy} recent {machine_label} connected and healthy."
    attention = "needs" if unhealthy == 1 else "need"
    return f"{healthy} of {total} recent {machine_label} healthy; {unhealthy} {attention} attention."


def _build_render_freshness_summary(
    observations: list[ClientRenderObservation],
    *,
    window: _Window,
    generated_at: datetime,
) -> ProductHealthCheckSummaryResponse:
    latest = max(
        (observation.observed_at for observation in observations if observation.observed_at is not None),
        default=None,
    )
    if latest is None:
        return ProductHealthCheckSummaryResponse(
            check=RENDER_FRESHNESS_CHECK_ID,
            verdict="unknown",
            coverage="none",
            window=window.label,
            generated_at=generated_at,
            headline=f"Render freshness has no signal; no render beacons arrived in the last {window.label}.",
        )

    age_seconds = max(0, int((generated_at - latest).total_seconds()))
    if age_seconds <= RENDER_FRESHNESS_OK_SECONDS:
        verdict = "ok"
        headline = f"Render beacons are fresh; latest arrived {_format_age(age_seconds)} ago."
    else:
        verdict = "degraded"
        headline = f"Render beacons are stale; latest arrived {_format_age(age_seconds)} ago."
    return ProductHealthCheckSummaryResponse(
        check=RENDER_FRESHNESS_CHECK_ID,
        verdict=verdict,
        coverage="full",
        window=window.label,
        generated_at=generated_at,
        headline=headline,
    )


def _summarize_live_preview_check(
    detail: ProductHealthCheckLivePreviewResponse,
) -> ProductHealthCheckSummaryResponse:
    verdict = _aggregate_verdict([cell.verdict for cell in detail.cells])
    coverage = _aggregate_coverage([cell.coverage for cell in detail.cells])
    return ProductHealthCheckSummaryResponse(
        check=detail.check,
        verdict=verdict,
        coverage=coverage,
        window=detail.window,
        generated_at=detail.generated_at,
        headline=_live_preview_headline(detail, verdict=verdict, coverage=coverage),
    )


def _aggregate_verdict(verdicts: list[str]) -> str:
    severity = {"unknown": 0, "ok": 1, "degraded": 2, "failing": 3}
    if not verdicts:
        return "unknown"
    return max(verdicts, key=lambda verdict: severity.get(verdict, -1))


def _aggregate_coverage(coverages: list[str]) -> str:
    if not coverages or all(coverage == "none" for coverage in coverages):
        return "none"
    if all(coverage == "full" for coverage in coverages):
        return "full"
    return "partial"


def _live_preview_headline(
    detail: ProductHealthCheckLivePreviewResponse,
    *,
    verdict: str,
    coverage: str,
) -> str:
    if coverage == "none":
        return "No live preview render observations in this window."

    worst = max(
        detail.cells,
        key=lambda cell: cell.signals.render_p95_ms or -1,
    )
    provider = worst.dimension.provider or "unknown provider"
    surface = worst.dimension.surface or "unknown surface"
    p95 = worst.signals.render_p95_ms
    if verdict == "failing":
        return f"{provider} {surface} live preview p95 is failing at {p95}ms."
    if verdict == "degraded":
        return f"{provider} {surface} live preview p95 is elevated at {p95}ms."
    if coverage == "partial":
        return "Live preview latency is within threshold, but coverage is partial."
    return "Live preview latency is within threshold."


def _parse_window(value: str) -> _Window:
    match = _WINDOW_RE.match(value or "")
    if not match:
        raise ValueError("Window must look like 15m, 1h, or 7d.")
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    seconds_by_unit = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}
    seconds = count * seconds_by_unit[unit]
    if seconds <= 0 or seconds > _MAX_WINDOW_SECONDS:
        raise ValueError("Window must be between 1 minute and 7 days.")
    return _Window(label=f"{count}{unit}", delta=timedelta(seconds=seconds))


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


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
