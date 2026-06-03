//! Rolling transport telemetry for engine ship attempts.
//!
//! This stays machine-local and cheap:
//! - bounded in-memory window
//! - one record per ship attempt
//! - summaries only, no payload content

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;

const SHIP_STATS_WINDOW: Duration = Duration::from_secs(60 * 60);
const SHIP_STATS_ACTIVE_WINDOW: Duration = Duration::from_secs(10 * 60);
const SHIP_STATS_MAX_RECORDS: usize = 50_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShipAttemptOutcome {
    Ok,
    RateLimited,
    ServerError,
    PayloadRejected,
    PayloadTooLarge,
    RetryableClientError,
    ConnectError,
}

impl ShipAttemptOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            ShipAttemptOutcome::Ok => "ok",
            ShipAttemptOutcome::RateLimited => "rate_limited",
            ShipAttemptOutcome::ServerError => "server_error",
            ShipAttemptOutcome::PayloadRejected => "payload_rejected",
            ShipAttemptOutcome::PayloadTooLarge => "payload_too_large",
            ShipAttemptOutcome::RetryableClientError => "retryable_client_error",
            ShipAttemptOutcome::ConnectError => "connect_error",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShipLane {
    Live,
    Repair,
    Archive,
    Unknown,
}

#[derive(Debug, Clone, Default)]
pub struct ShipStageTimings {
    pub observed_at_ms: Option<i64>,
    pub latest_observed_at_ms: Option<i64>,
    pub http_send_started_at_ms: Option<i64>,
    pub http_finished_at_ms: Option<i64>,
    pub observation_window_ms: Option<u64>,
    pub observation_to_enqueue_ms: Option<u64>,
    pub observation_to_wake_ms: Option<u64>,
    pub wake_to_enqueue_ms: Option<u64>,
    pub enqueue_to_job_ms: Option<u64>,
    pub observed_to_job_ms: Option<u64>,
    pub prepare_ms: Option<u64>,
    pub job_to_http_ms: Option<u64>,
    pub observed_to_http_send_ms: Option<u64>,
    pub http_latency_ms: Option<u64>,
    pub job_to_ack_ms: Option<u64>,
    pub observed_to_ack_ms: Option<u64>,
}

impl ShipStageTimings {
    fn latency_fields(&self) -> [(&'static str, Option<u64>); 12] {
        [
            ("observation_window_ms", self.observation_window_ms),
            ("observation_to_enqueue_ms", self.observation_to_enqueue_ms),
            ("observation_to_wake_ms", self.observation_to_wake_ms),
            ("wake_to_enqueue_ms", self.wake_to_enqueue_ms),
            ("enqueue_to_job_ms", self.enqueue_to_job_ms),
            ("observed_to_job_ms", self.observed_to_job_ms),
            ("prepare_ms", self.prepare_ms),
            ("job_to_http_ms", self.job_to_http_ms),
            ("observed_to_http_send_ms", self.observed_to_http_send_ms),
            ("http_latency_ms", self.http_latency_ms),
            ("job_to_ack_ms", self.job_to_ack_ms),
            ("observed_to_ack_ms", self.observed_to_ack_ms),
        ]
    }
}

#[derive(Debug, Clone)]
struct ShipAttemptRecord {
    at: Instant,
    recorded_at: String,
    lane: ShipLane,
    outcome: ShipAttemptOutcome,
    latency_ms: u64,
    http_status: Option<u16>,
    error_kind: Option<String>,
    error_message: Option<String>,
    event_count: u32,
    byte_count: u64,
    is_backpressure: bool,
    stage_timings: Option<ShipStageTimings>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ShipLaneStatsSummary {
    pub attempts_1h: u32,
    pub successes_1h: u32,
    pub server_errors_1h: u32,
    pub connect_errors_1h: u32,
    pub backpressure_1h: u32,
    pub events_1h: u64,
    pub bytes_1h: u64,
    pub attempts_10m: u32,
    pub successes_10m: u32,
    pub server_errors_10m: u32,
    pub connect_errors_10m: u32,
    pub backpressure_10m: u32,
    pub events_10m: u64,
    pub bytes_10m: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latency_p50_ms_1h: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latency_p95_ms_1h: Option<u64>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub stage_latency_p50_ms_1h: BTreeMap<String, u64>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub stage_latency_p95_ms_1h: BTreeMap<String, u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_attempt_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_success_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_observed_at_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_latest_observed_at_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_http_send_started_at_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_http_finished_at_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub events_per_sec_ewma_10s: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes_per_sec_ewma_10s: Option<f64>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ShipLaneSummarySet {
    pub live: ShipLaneStatsSummary,
    pub repair: ShipLaneStatsSummary,
    pub archive: ShipLaneStatsSummary,
    pub unknown: ShipLaneStatsSummary,
}

#[derive(Debug, Clone, Default)]
pub struct ShipStatsSummary {
    pub last_ship_attempt_at: Option<String>,
    pub last_ship_result: Option<String>,
    pub last_ship_latency_ms: Option<u64>,
    pub last_ship_http_status: Option<u16>,
    pub last_ship_error_kind: Option<String>,
    pub last_ship_error_message: Option<String>,
    pub ship_attempts_1h: u32,
    pub ship_successes_1h: u32,
    pub ship_rate_limited_1h: u32,
    pub ship_server_errors_1h: u32,
    pub ship_payload_rejections_1h: u32,
    pub ship_payload_too_large_1h: u32,
    pub ship_retryable_client_errors_1h: u32,
    pub ship_connect_errors_1h: u32,
    pub ship_latency_p50_ms_1h: Option<u64>,
    pub ship_latency_p95_ms_1h: Option<u64>,
    pub ship_attempts_10m: u32,
    pub ship_successes_10m: u32,
    pub ship_rate_limited_10m: u32,
    pub ship_server_errors_10m: u32,
    pub ship_retryable_client_errors_10m: u32,
    pub ship_connect_errors_10m: u32,
    /// Phase 1 instrumentation: EWMA events/sec over the last ~10s of
    /// successful ship attempts. Drives the phase 2 adaptive controller
    /// and the bench harness's "events shipped per second" axis.
    pub events_per_sec_ewma_10s: Option<f64>,
    pub bytes_per_sec_ewma_10s: Option<f64>,
    pub lanes: ShipLaneSummarySet,
}

/// EWMA throughput tracker for successful ship events.
///
/// Phase 1 instrumentation: a 10s exponential moving average on
/// events-per-second computed from successful ship attempts. The phase 2
/// adaptive controller reads this together with server-side queue_wait_ms
/// to decide whether to grow or shrink in-flight requests.
#[derive(Debug, Clone, Default)]
struct EwmaThroughput {
    /// Last update timestamp (None until first sample).
    last_at: Option<Instant>,
    /// Current EWMA estimate of events/sec.
    ewma_eps: f64,
    /// Current EWMA estimate of uncompressed transcript bytes/sec.
    ewma_bps: f64,
}

impl EwmaThroughput {
    /// 10s time-constant — half-life ≈ 6.9s. New samples weighted by the
    /// elapsed-time fraction so bursty/idle gaps do not double-count.
    const TIME_CONSTANT_SECS: f64 = 10.0;

    fn record(&mut self, now: Instant, events: u32, bytes: u64, latency_ms: u64) {
        if events == 0 && bytes == 0 {
            return;
        }
        // Prefer the actual ship duration (latency_ms) for the instantaneous
        // rate, falling back to wall-clock elapsed since the last sample.
        let dt_secs = (latency_ms.max(1) as f64) / 1000.0;
        let instantaneous_eps = (events as f64) / dt_secs;
        let instantaneous_bps = (bytes as f64) / dt_secs;

        let alpha = match self.last_at {
            None => 1.0,
            Some(prev) => {
                let elapsed = now.saturating_duration_since(prev).as_secs_f64();
                1.0 - (-elapsed / Self::TIME_CONSTANT_SECS).exp()
            }
        };
        self.ewma_eps = alpha * instantaneous_eps + (1.0 - alpha) * self.ewma_eps;
        self.ewma_bps = alpha * instantaneous_bps + (1.0 - alpha) * self.ewma_bps;
        self.last_at = Some(now);
    }

    fn current(&self, now: Instant) -> (Option<f64>, Option<f64>) {
        let Some(last) = self.last_at else {
            return (None, None);
        };
        // Decay the estimate toward zero if no updates have arrived recently
        // so a long idle period doesn't keep reporting stale throughput.
        let elapsed = now.saturating_duration_since(last).as_secs_f64();
        let decay = (-elapsed / Self::TIME_CONSTANT_SECS).exp();
        let eps = self.ewma_eps * decay;
        let bps = self.ewma_bps * decay;
        (
            eps.is_finite().then_some(eps),
            bps.is_finite().then_some(bps),
        )
    }
}

#[derive(Clone, Default)]
pub struct RecentShipStatsTracker {
    inner: Arc<Mutex<VecDeque<ShipAttemptRecord>>>,
    throughput: Arc<Mutex<EwmaThroughput>>,
    live_throughput: Arc<Mutex<EwmaThroughput>>,
    repair_throughput: Arc<Mutex<EwmaThroughput>>,
    archive_throughput: Arc<Mutex<EwmaThroughput>>,
    unknown_throughput: Arc<Mutex<EwmaThroughput>>,
}

impl RecentShipStatsTracker {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::new())),
            throughput: Arc::new(Mutex::new(EwmaThroughput::default())),
            live_throughput: Arc::new(Mutex::new(EwmaThroughput::default())),
            repair_throughput: Arc::new(Mutex::new(EwmaThroughput::default())),
            archive_throughput: Arc::new(Mutex::new(EwmaThroughput::default())),
            unknown_throughput: Arc::new(Mutex::new(EwmaThroughput::default())),
        }
    }

    #[cfg(test)]
    pub fn record(&self, outcome: ShipAttemptOutcome, latency_ms: u64, http_status: Option<u16>) {
        self.record_with_detail(outcome, latency_ms, http_status, None, None);
    }

    /// Record events shipped on a successful attempt for EWMA throughput.
    /// Call this AFTER `record(...)` for `ShipAttemptOutcome::Ok` ships.
    #[cfg(test)]
    pub fn record_events_shipped(&self, events: u32, latency_ms: u64) {
        self.record_events_and_bytes_shipped(ShipLane::Unknown, events, 0, latency_ms);
    }

    pub fn record_events_and_bytes_shipped(
        &self,
        lane: ShipLane,
        events: u32,
        bytes: u64,
        latency_ms: u64,
    ) {
        let now = Instant::now();
        if let Ok(mut t) = self.throughput.lock() {
            t.record(now, events, bytes, latency_ms);
        }
        let lane_throughput = match lane {
            ShipLane::Live => &self.live_throughput,
            ShipLane::Repair => &self.repair_throughput,
            ShipLane::Archive => &self.archive_throughput,
            ShipLane::Unknown => &self.unknown_throughput,
        };
        if let Ok(mut t) = lane_throughput.lock() {
            t.record(now, events, bytes, latency_ms);
        }
    }

    #[cfg(test)]
    pub fn record_with_detail(
        &self,
        outcome: ShipAttemptOutcome,
        latency_ms: u64,
        http_status: Option<u16>,
        error_kind: Option<&str>,
        error_message: Option<&str>,
    ) {
        self.record_at(
            Instant::now(),
            chrono::Utc::now().to_rfc3339(),
            ShipLane::Unknown,
            outcome,
            latency_ms,
            http_status,
            error_kind.map(str::to_string),
            error_message.map(truncate_error_message),
            0,
            0,
            false,
            None,
        );
    }

    #[cfg(test)]
    #[allow(clippy::too_many_arguments)]
    pub fn record_with_lane_and_detail(
        &self,
        lane: ShipLane,
        outcome: ShipAttemptOutcome,
        latency_ms: u64,
        http_status: Option<u16>,
        error_kind: Option<&str>,
        error_message: Option<&str>,
        event_count: u32,
        byte_count: u64,
        is_backpressure: bool,
    ) {
        self.record_with_lane_detail_and_stages(
            lane,
            outcome,
            latency_ms,
            http_status,
            error_kind,
            error_message,
            event_count,
            byte_count,
            is_backpressure,
            None,
        );
    }

    #[allow(clippy::too_many_arguments)]
    pub fn record_with_lane_detail_and_stages(
        &self,
        lane: ShipLane,
        outcome: ShipAttemptOutcome,
        latency_ms: u64,
        http_status: Option<u16>,
        error_kind: Option<&str>,
        error_message: Option<&str>,
        event_count: u32,
        byte_count: u64,
        is_backpressure: bool,
        stage_timings: Option<ShipStageTimings>,
    ) {
        self.record_at(
            Instant::now(),
            chrono::Utc::now().to_rfc3339(),
            lane,
            outcome,
            latency_ms,
            http_status,
            error_kind.map(str::to_string),
            error_message.map(truncate_error_message),
            event_count,
            byte_count,
            is_backpressure,
            stage_timings,
        );
        if matches!(outcome, ShipAttemptOutcome::Ok) {
            self.record_events_and_bytes_shipped(lane, event_count, byte_count, latency_ms);
        }
    }

    pub fn summary(&self) -> ShipStatsSummary {
        let now = Instant::now();
        if let Ok(mut guard) = self.inner.lock() {
            prune_old_records(&mut guard, now);
            if guard.is_empty() {
                return ShipStatsSummary::default();
            }

            let mut summary = ShipStatsSummary::default();
            let mut latencies = Vec::with_capacity(guard.len());
            let mut lane_acc = LaneAccumulators::default();

            for record in guard.iter() {
                if !record.is_backpressure {
                    summary.ship_attempts_1h += 1;
                    latencies.push(record.latency_ms);
                }
                let in_active_window = now.duration_since(record.at) <= SHIP_STATS_ACTIVE_WINDOW;
                if in_active_window && !record.is_backpressure {
                    summary.ship_attempts_10m += 1;
                }
                lane_acc.record(record, in_active_window);
                match record.outcome {
                    ShipAttemptOutcome::Ok => {
                        if !record.is_backpressure {
                            summary.ship_successes_1h += 1;
                        }
                        if in_active_window && !record.is_backpressure {
                            summary.ship_successes_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::RateLimited => {
                        if !record.is_backpressure {
                            summary.ship_rate_limited_1h += 1;
                        }
                        if in_active_window && !record.is_backpressure {
                            summary.ship_rate_limited_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::ServerError => {
                        if !record.is_backpressure {
                            summary.ship_server_errors_1h += 1;
                        }
                        if in_active_window && !record.is_backpressure {
                            summary.ship_server_errors_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::PayloadRejected => {
                        if !record.is_backpressure {
                            summary.ship_payload_rejections_1h += 1;
                        }
                    }
                    ShipAttemptOutcome::PayloadTooLarge => {
                        if !record.is_backpressure {
                            summary.ship_payload_too_large_1h += 1;
                        }
                    }
                    ShipAttemptOutcome::RetryableClientError => {
                        if !record.is_backpressure {
                            summary.ship_retryable_client_errors_1h += 1;
                        }
                        if in_active_window && !record.is_backpressure {
                            summary.ship_retryable_client_errors_10m += 1;
                        }
                    }
                    ShipAttemptOutcome::ConnectError => {
                        if !record.is_backpressure {
                            summary.ship_connect_errors_1h += 1;
                        }
                        if in_active_window && !record.is_backpressure {
                            summary.ship_connect_errors_10m += 1;
                        }
                    }
                }
            }

            if let Some(last) = guard.back() {
                summary.last_ship_attempt_at = Some(last.recorded_at.clone());
                summary.last_ship_result = Some(last.outcome.as_str().to_string());
                summary.last_ship_latency_ms = Some(last.latency_ms);
                summary.last_ship_http_status = last.http_status;
                summary.last_ship_error_kind = last.error_kind.clone();
                summary.last_ship_error_message = last.error_message.clone();
            }

            latencies.sort_unstable();
            summary.ship_latency_p50_ms_1h = percentile(&latencies, 0.50);
            summary.ship_latency_p95_ms_1h = percentile(&latencies, 0.95);
            if let Ok(t) = self.throughput.lock() {
                (
                    summary.events_per_sec_ewma_10s,
                    summary.bytes_per_sec_ewma_10s,
                ) = t.current(now);
            }
            summary.lanes = lane_acc.finish(self, now);
            summary
        } else {
            ShipStatsSummary::default()
        }
    }

    fn record_at(
        &self,
        at: Instant,
        recorded_at: String,
        lane: ShipLane,
        outcome: ShipAttemptOutcome,
        latency_ms: u64,
        http_status: Option<u16>,
        error_kind: Option<String>,
        error_message: Option<String>,
        event_count: u32,
        byte_count: u64,
        is_backpressure: bool,
        stage_timings: Option<ShipStageTimings>,
    ) {
        if let Ok(mut guard) = self.inner.lock() {
            guard.push_back(ShipAttemptRecord {
                at,
                recorded_at,
                lane,
                outcome,
                latency_ms,
                http_status,
                error_kind,
                error_message,
                event_count,
                byte_count,
                is_backpressure,
                stage_timings,
            });
            prune_old_records(&mut guard, at);
            while guard.len() > SHIP_STATS_MAX_RECORDS {
                guard.pop_front();
            }
        }
    }
}

#[derive(Default)]
struct LaneAccumulators {
    live: LaneAccumulator,
    repair: LaneAccumulator,
    archive: LaneAccumulator,
    unknown: LaneAccumulator,
}

impl LaneAccumulators {
    fn record(&mut self, record: &ShipAttemptRecord, in_active_window: bool) {
        match record.lane {
            ShipLane::Live => self.live.record(record, in_active_window),
            ShipLane::Repair => self.repair.record(record, in_active_window),
            ShipLane::Archive => self.archive.record(record, in_active_window),
            ShipLane::Unknown => self.unknown.record(record, in_active_window),
        }
    }

    fn finish(self, tracker: &RecentShipStatsTracker, now: Instant) -> ShipLaneSummarySet {
        ShipLaneSummarySet {
            live: self.live.finish(&tracker.live_throughput, now),
            repair: self.repair.finish(&tracker.repair_throughput, now),
            archive: self.archive.finish(&tracker.archive_throughput, now),
            unknown: self.unknown.finish(&tracker.unknown_throughput, now),
        }
    }
}

#[derive(Default)]
struct LaneAccumulator {
    summary: ShipLaneStatsSummary,
    latencies: Vec<u64>,
    stage_latencies: BTreeMap<String, Vec<u64>>,
}

impl LaneAccumulator {
    fn record(&mut self, record: &ShipAttemptRecord, in_active_window: bool) {
        self.summary.attempts_1h += 1;
        self.summary.last_attempt_at = Some(record.recorded_at.clone());
        self.summary.events_1h += u64::from(record.event_count);
        self.summary.bytes_1h += record.byte_count;
        self.latencies.push(record.latency_ms);
        if let Some(stages) = &record.stage_timings {
            self.summary.last_observed_at_ms = stages.observed_at_ms;
            self.summary.last_latest_observed_at_ms = stages.latest_observed_at_ms;
            self.summary.last_http_send_started_at_ms = stages.http_send_started_at_ms;
            self.summary.last_http_finished_at_ms = stages.http_finished_at_ms;
            for (field, value) in stages.latency_fields() {
                if let Some(ms) = value {
                    self.stage_latencies
                        .entry(field.to_string())
                        .or_default()
                        .push(ms);
                }
            }
        }
        if record.is_backpressure {
            self.summary.backpressure_1h += 1;
        }
        if in_active_window {
            self.summary.attempts_10m += 1;
            self.summary.events_10m += u64::from(record.event_count);
            self.summary.bytes_10m += record.byte_count;
            if record.is_backpressure {
                self.summary.backpressure_10m += 1;
            }
        }
        match record.outcome {
            ShipAttemptOutcome::Ok => {
                self.summary.successes_1h += 1;
                self.summary.last_success_at = Some(record.recorded_at.clone());
                if in_active_window {
                    self.summary.successes_10m += 1;
                }
            }
            ShipAttemptOutcome::ServerError => {
                self.summary.server_errors_1h += 1;
                if in_active_window {
                    self.summary.server_errors_10m += 1;
                }
            }
            ShipAttemptOutcome::ConnectError => {
                self.summary.connect_errors_1h += 1;
                if in_active_window {
                    self.summary.connect_errors_10m += 1;
                }
            }
            ShipAttemptOutcome::RateLimited
            | ShipAttemptOutcome::PayloadRejected
            | ShipAttemptOutcome::PayloadTooLarge
            | ShipAttemptOutcome::RetryableClientError => {}
        }
    }

    fn finish(
        mut self,
        throughput: &Arc<Mutex<EwmaThroughput>>,
        now: Instant,
    ) -> ShipLaneStatsSummary {
        self.latencies.sort_unstable();
        self.summary.latency_p50_ms_1h = percentile(&self.latencies, 0.50);
        self.summary.latency_p95_ms_1h = percentile(&self.latencies, 0.95);
        for (field, mut values) in self.stage_latencies {
            values.sort_unstable();
            if let Some(value) = percentile(&values, 0.50) {
                self.summary
                    .stage_latency_p50_ms_1h
                    .insert(field.clone(), value);
            }
            if let Some(value) = percentile(&values, 0.95) {
                self.summary.stage_latency_p95_ms_1h.insert(field, value);
            }
        }
        if let Ok(t) = throughput.lock() {
            (
                self.summary.events_per_sec_ewma_10s,
                self.summary.bytes_per_sec_ewma_10s,
            ) = t.current(now);
        }
        self.summary
    }
}

fn truncate_error_message(message: &str) -> String {
    const MAX_ERROR_CHARS: usize = 300;
    let mut chars = message.chars();
    let truncated: String = chars.by_ref().take(MAX_ERROR_CHARS).collect();
    if chars.next().is_some() {
        format!("{truncated}...")
    } else {
        truncated
    }
}

fn prune_old_records(records: &mut VecDeque<ShipAttemptRecord>, now: Instant) {
    while let Some(front) = records.front() {
        if now.saturating_duration_since(front.at) > SHIP_STATS_WINDOW {
            records.pop_front();
        } else {
            break;
        }
    }
}

fn percentile(values: &[u64], quantile: f64) -> Option<u64> {
    if values.is_empty() {
        return None;
    }
    let clamped = quantile.clamp(0.0, 1.0);
    let scaled = (values.len().saturating_sub(1) as f64) * clamped;
    let lower_idx = scaled.floor() as usize;
    let upper_idx = scaled.ceil() as usize;
    let lower = *values.get(lower_idx)?;
    let upper = *values.get(upper_idx)?;
    if lower_idx == upper_idx {
        return Some(lower);
    }
    let weight = scaled - lower_idx as f64;
    Some(((lower as f64) + ((upper as f64) - (lower as f64)) * weight).round() as u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[allow(clippy::too_many_arguments)]
    fn record_test(
        tracker: &RecentShipStatsTracker,
        at: Instant,
        recorded_at: &str,
        outcome: ShipAttemptOutcome,
        latency_ms: u64,
        http_status: Option<u16>,
        error_kind: Option<&str>,
        error_message: Option<&str>,
    ) {
        tracker.record_at(
            at,
            recorded_at.to_string(),
            ShipLane::Unknown,
            outcome,
            latency_ms,
            http_status,
            error_kind.map(str::to_string),
            error_message.map(str::to_string),
            0,
            0,
            false,
            None,
        );
    }

    #[test]
    fn recent_ship_stats_summary_counts_outcomes_and_percentiles() {
        let tracker = RecentShipStatsTracker::new();
        let now = Instant::now();
        record_test(
            &tracker,
            now - Duration::from_secs(10),
            "2026-04-23T20:00:00Z",
            ShipAttemptOutcome::Ok,
            40,
            None,
            None,
            None,
        );
        record_test(
            &tracker,
            now - Duration::from_secs(9),
            "2026-04-23T20:00:01Z",
            ShipAttemptOutcome::Ok,
            60,
            None,
            None,
            None,
        );
        record_test(
            &tracker,
            now - Duration::from_secs(8),
            "2026-04-23T20:00:02Z",
            ShipAttemptOutcome::ServerError,
            120,
            Some(503),
            Some("server_response"),
            Some("503: upstream unavailable"),
        );
        record_test(
            &tracker,
            now - Duration::from_secs(7),
            "2026-04-23T20:00:03Z",
            ShipAttemptOutcome::ConnectError,
            220,
            None,
            Some("timeout"),
            Some("request timed out after 60s"),
        );

        let summary = tracker.summary();

        assert_eq!(summary.ship_attempts_1h, 4);
        assert_eq!(summary.ship_successes_1h, 2);
        assert_eq!(summary.ship_server_errors_1h, 1);
        assert_eq!(summary.ship_connect_errors_1h, 1);
        assert_eq!(summary.ship_attempts_10m, 4);
        assert_eq!(summary.ship_successes_10m, 2);
        assert_eq!(summary.ship_server_errors_10m, 1);
        assert_eq!(summary.ship_connect_errors_10m, 1);
        assert_eq!(
            summary.last_ship_attempt_at.as_deref(),
            Some("2026-04-23T20:00:03Z")
        );
        assert_eq!(summary.last_ship_result.as_deref(), Some("connect_error"));
        assert_eq!(summary.last_ship_latency_ms, Some(220));
        assert_eq!(summary.last_ship_http_status, None);
        assert_eq!(summary.last_ship_error_kind.as_deref(), Some("timeout"));
        assert_eq!(
            summary.last_ship_error_message.as_deref(),
            Some("request timed out after 60s")
        );
        assert_eq!(summary.ship_latency_p50_ms_1h, Some(90));
        assert_eq!(summary.ship_latency_p95_ms_1h, Some(205));
    }

    #[test]
    fn recent_ship_stats_prunes_old_records() {
        let tracker = RecentShipStatsTracker::new();
        let now = Instant::now();
        record_test(
            &tracker,
            now - Duration::from_secs(SHIP_STATS_WINDOW.as_secs() + 60),
            "2026-04-23T18:00:00Z",
            ShipAttemptOutcome::Ok,
            50,
            None,
            None,
            None,
        );
        record_test(
            &tracker,
            now - Duration::from_secs(30),
            "2026-04-23T19:59:30Z",
            ShipAttemptOutcome::RateLimited,
            100,
            Some(429),
            Some("rate_limited"),
            Some("429: rate limited"),
        );

        let summary = tracker.summary();

        assert_eq!(summary.ship_attempts_1h, 1);
        assert_eq!(summary.ship_rate_limited_1h, 1);
        assert_eq!(summary.ship_successes_1h, 0);
        assert_eq!(summary.ship_attempts_10m, 1);
        assert_eq!(summary.ship_rate_limited_10m, 1);
        assert_eq!(summary.ship_successes_10m, 0);
        assert_eq!(
            summary.last_ship_attempt_at.as_deref(),
            Some("2026-04-23T19:59:30Z")
        );
        assert_eq!(summary.last_ship_result.as_deref(), Some("rate_limited"));
        assert_eq!(summary.last_ship_http_status, Some(429));
        assert_eq!(
            summary.last_ship_error_kind.as_deref(),
            Some("rate_limited")
        );
    }

    #[test]
    fn recent_ship_stats_truncates_last_error_message() {
        let tracker = RecentShipStatsTracker::new();
        tracker.record_with_detail(
            ShipAttemptOutcome::ConnectError,
            60_000,
            None,
            Some("timeout"),
            Some(&"x".repeat(350)),
        );

        let summary = tracker.summary();

        let message = summary.last_ship_error_message.unwrap();
        assert_eq!(message.chars().count(), 303);
        assert!(message.ends_with("..."));
    }

    #[test]
    fn ewma_throughput_first_sample_uses_latency_for_instantaneous_rate() {
        let mut tput = EwmaThroughput::default();
        let t0 = Instant::now();
        tput.record(t0, 100, 10_000, 100); // 100 events in 100ms => 1000 eps
        let (eps, bps) = tput.current(t0);
        let v = eps.unwrap();
        assert!((v - 1000.0).abs() < 1e-6, "expected 1000 eps, got {v}");
        let bv = bps.unwrap();
        assert!(
            (bv - 100_000.0).abs() < 1e-6,
            "expected 100000 bps, got {bv}"
        );
    }

    #[test]
    fn ewma_throughput_decays_toward_zero_during_idle() {
        let mut tput = EwmaThroughput::default();
        let t0 = Instant::now();
        tput.record(t0, 100, 10_000, 100); // 1000 eps initial
                                           // 60s later, with TIME_CONSTANT 10s, decay factor ≈ e^-6 ≈ 0.0025
        let later = t0 + Duration::from_secs(60);
        let v = tput.current(later).0.unwrap();
        assert!(v < 5.0, "expected near-zero after 60s idle, got {v}");
    }

    #[test]
    fn record_events_shipped_surfaces_in_summary() {
        let tracker = RecentShipStatsTracker::new();
        // Record one successful ship of 200 events that took 50ms => 4000 eps.
        tracker.record(ShipAttemptOutcome::Ok, 50, None);
        tracker.record_events_shipped(200, 50);
        let summary = tracker.summary();
        let eps = summary.events_per_sec_ewma_10s.unwrap();
        // The summary call may have advanced Instant::now() slightly, so we
        // accept a small decay band rather than equality with 4000.
        assert!(
            eps > 3500.0 && eps <= 4000.5,
            "expected ~4000 eps, got {eps}"
        );
    }

    #[test]
    fn record_events_shipped_zero_events_is_a_noop() {
        let tracker = RecentShipStatsTracker::new();
        tracker.record_events_shipped(0, 50);
        let summary = tracker.summary();
        assert_eq!(summary.events_per_sec_ewma_10s, None);
    }

    #[test]
    fn lane_stats_count_archive_backpressure_without_poisoning_aggregate() {
        let tracker = RecentShipStatsTracker::new();
        tracker.record_with_lane_and_detail(
            ShipLane::Archive,
            ShipAttemptOutcome::Ok,
            100,
            Some(200),
            None,
            None,
            50,
            5_000,
            false,
        );
        tracker.record_with_lane_and_detail(
            ShipLane::Archive,
            ShipAttemptOutcome::ServerError,
            30,
            Some(503),
            Some("runtime_backpressure"),
            Some("runtime queue full"),
            0,
            0,
            true,
        );

        let summary = tracker.summary();

        assert_eq!(summary.ship_attempts_1h, 1);
        assert_eq!(summary.ship_successes_1h, 1);
        assert_eq!(summary.ship_server_errors_1h, 0);
        assert_eq!(summary.lanes.archive.attempts_1h, 2);
        assert_eq!(summary.lanes.archive.successes_1h, 1);
        assert_eq!(summary.lanes.archive.server_errors_1h, 1);
        assert_eq!(summary.lanes.archive.backpressure_1h, 1);
        assert_eq!(summary.lanes.archive.events_1h, 50);
        assert_eq!(summary.lanes.archive.bytes_1h, 5_000);
        assert!(summary.lanes.archive.events_per_sec_ewma_10s.is_some());
        assert!(summary.lanes.archive.bytes_per_sec_ewma_10s.is_some());
    }

    #[test]
    fn lane_stats_surface_live_stage_percentiles() {
        let tracker = RecentShipStatsTracker::new();
        tracker.record_with_lane_detail_and_stages(
            ShipLane::Live,
            ShipAttemptOutcome::Ok,
            100,
            Some(200),
            None,
            None,
            10,
            2_048,
            false,
            Some(ShipStageTimings {
                observed_at_ms: Some(1_000),
                latest_observed_at_ms: Some(1_015),
                http_send_started_at_ms: Some(1_080),
                http_finished_at_ms: Some(1_100),
                observation_window_ms: Some(15),
                observation_to_enqueue_ms: Some(20),
                observation_to_wake_ms: Some(5),
                wake_to_enqueue_ms: Some(15),
                enqueue_to_job_ms: Some(30),
                observed_to_job_ms: Some(50),
                prepare_ms: Some(10),
                job_to_http_ms: Some(30),
                observed_to_http_send_ms: Some(80),
                http_latency_ms: Some(20),
                job_to_ack_ms: Some(50),
                observed_to_ack_ms: Some(100),
            }),
        );

        let summary = tracker.summary();
        let live = summary.lanes.live;

        assert_eq!(live.last_observed_at_ms, Some(1_000));
        assert_eq!(live.last_latest_observed_at_ms, Some(1_015));
        assert_eq!(live.last_http_send_started_at_ms, Some(1_080));
        assert_eq!(live.last_http_finished_at_ms, Some(1_100));
        assert_eq!(
            live.stage_latency_p50_ms_1h
                .get("observed_to_ack_ms")
                .copied(),
            Some(100)
        );
        assert_eq!(
            live.stage_latency_p95_ms_1h
                .get("observed_to_http_send_ms")
                .copied(),
            Some(80)
        );
        assert_eq!(
            live.stage_latency_p95_ms_1h
                .get("observation_to_enqueue_ms")
                .copied(),
            Some(20)
        );
    }

    #[test]
    fn recent_ship_stats_keeps_active_window_separate_from_one_hour_window() {
        let tracker = RecentShipStatsTracker::new();
        let now = Instant::now();
        record_test(
            &tracker,
            now - Duration::from_secs(20 * 60),
            "2026-04-23T19:40:00Z",
            ShipAttemptOutcome::ConnectError,
            3_000,
            None,
            Some("timeout"),
            Some("request timed out"),
        );
        record_test(
            &tracker,
            now - Duration::from_secs(60),
            "2026-04-23T19:59:00Z",
            ShipAttemptOutcome::Ok,
            200,
            None,
            None,
            None,
        );

        let summary = tracker.summary();

        assert_eq!(summary.ship_attempts_1h, 2);
        assert_eq!(summary.ship_connect_errors_1h, 1);
        assert_eq!(summary.ship_attempts_10m, 1);
        assert_eq!(summary.ship_connect_errors_10m, 0);
        assert_eq!(summary.ship_successes_10m, 1);
    }
}
